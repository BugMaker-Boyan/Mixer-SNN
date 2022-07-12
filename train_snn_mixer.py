import argparse
import random
import time
import warnings
import datetime

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
import torch.utils.data
from torch.utils.tensorboard import SummaryWriter
import numpy as np
import os
import sys

import torchvision.datasets

import utils

from spikingjelly.activation_based.encoding import PoissonEncoder
from spikingjelly.activation_based import functional
from models.configs import get_mixer_config
from models.model import MlpMixer


def set_deterministic(_seed_: int = 2022):
    random.seed(_seed_)
    np.random.seed(_seed_)
    torch.manual_seed(_seed_)
    torch.cuda.manual_seed_all(_seed_)
    cudnn.deterministic = True
    cudnn.benchmark = False


class Trainer(object):
    def __init__(self):
        self.encoder = PoissonEncoder()
        self.model_configs = {
            'mixer': get_mixer_config()
        }

    def main(self, args):
        set_deterministic(args.seed)
        if args.output_dir:
            os.makedirs(args.output_dir, exist_ok=True)

        utils.init_distributed_mode(args)
        print(args)

        device = torch.device(args.device)

        dataset_train, dataset_test, train_sampler, test_sampler = self.load_data(args)

        num_classes = len(dataset_train.classes)

        args.num_classes = num_classes

        dataloader_train = torch.utils.data.DataLoader(
            dataset=dataset_train,
            batch_size=args.batch_size,
            sampler=train_sampler,
            num_workers=args.workers,
            pin_memory=True,
        )

        dataloader_test = torch.utils.data.DataLoader(
            dataset=dataset_test,
            batch_size=args.batch_size,
            sampler=test_sampler,
            num_workers=args.workers,
            pin_memory=True,
        )

        print('Creating model...')
        model = self.load_model(args, num_classes)
        model.to(device)
        print(model)

        if args.sync_bn:
            model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

        criterion = nn.MSELoss()

        optimizer = self.set_optimizer(args, model.parameters())

        if args.amp:
            scaler = torch.cuda.amp.GradScaler()
        else:
            scaler = None

        lr_scheduler = self.set_lr_scheduler(args, optimizer)

        model_without_ddp = model
        if args.distributed:
            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
            model_without_ddp = model.module

        log_dir = self.get_logdir_name(args)
        pt_dir = os.path.join(args.output_dir, log_dir, 'pt')
        tb_dir = os.path.join(args.output_dir, log_dir, 'tb')
        print(log_dir)

        if utils.is_main_process():
            os.makedirs(tb_dir, exist_ok=args.resume is not None)
            os.makedirs(pt_dir, exist_ok=args.resume is not None)

        max_test_acc1 = -1.
        if args.resume is not None:
            if args.resume == 'latest':
                checkpoint = torch.load(os.path.join(pt_dir, 'checkpoint_latest.pth'), map_location='cpu')
            else:
                checkpoint = torch.load(args.resume, map_location='cpu')
            model_without_ddp.load_state_dict(checkpoint['model'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
            args.start_epoch = checkpoint['epoch'] + 1
            if scaler:
                scaler.load_state_dict(checkpoint['scaler'])

            if utils.is_main_process():
                max_test_acc1 = checkpoint['max_test_acc1']

        if utils.is_main_process():
            tb_writer = SummaryWriter(tb_dir, purge_step=args.start_epoch)
            with open(os.path.join(args.output_dir, log_dir, 'args.txt'), 'w', encoding='utf-8') as args_txt:
                args_txt.write(str(args))
                args_txt.write('\n')
                args_txt.write(' '.join(sys.argv))

        for epoch in range(args.start_epoch, args.epochs):
            start_time = time.time()
            if args.distributed:
                train_sampler.set_epoch(epoch)

            train_loss, train_acc1, train_acc5 = self.train_one_epoch(model, criterion, optimizer, dataloader_train, device, epoch, args, scaler)
            if utils.is_main_process():
                tb_writer.add_scalar('train_loss', train_loss, epoch)
                tb_writer.add_scalar('train_acc1', train_acc1, epoch)
                tb_writer.add_scalar('train_acc5', train_acc5, epoch)

            lr_scheduler.step()
            test_loss, test_acc1, test_acc5 = self.evaluate(args, model, criterion, dataloader_test, device)
            if utils.is_main_process():
                tb_writer.add_scalar('test_loss', test_loss, epoch)
                tb_writer.add_scalar('test_acc1', test_acc1, epoch)
                tb_writer.add_scalar('test_acc5', test_acc5, epoch)

            if utils.is_main_process():
                save_max_test_acc1 = False
                if test_acc1 > max_test_acc1:
                    max_test_acc1 = test_acc1
                    save_max_test_acc1 = True
                checkpoint = {
                    "model": model_without_ddp.state_dict(),
                    "optimizer": optimizer.state_dict(),
                    "lr_scheduler": lr_scheduler.state_dict(),
                    "epoch": epoch,
                    "args": args,
                    "max_test_acc1": max_test_acc1,
                }
                if scaler:
                    checkpoint["scaler"] = scaler.state_dict()
                utils.save_on_master(checkpoint, os.path.join(pt_dir, "checkpoint_latest.pth"))
                if save_max_test_acc1:
                    utils.save_on_master(checkpoint, os.path.join(pt_dir, f"checkpoint_max_test_acc1.pth"))

            print(f'escape time={(datetime.datetime.now() + datetime.timedelta(seconds=(time.time() - start_time) * (args.epochs - epoch))).strftime("%Y-%m-%d %H:%M:%S")}\n')
            print(args)

    def train_one_epoch(self, model, criterion, optimizer, data_loader, device, epoch, args, scaler):
        model.train()
        metric_logger = utils.MetricLogger(delimiter=' ')
        metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value}'))
        metric_logger.add_meter('img/s', utils.SmoothedValue(window_size=10, fmt='{value}'))

        header = f'Epoch: [{epoch}]'
        for i, (img, target) in enumerate(metric_logger.log_every(data_loader, -1, header)):
            start_time = time.time()
            img, target = img.to(device), F.one_hot(target, num_classes=args.num_classes).float().to(device)
            with torch.cuda.amp.autocast(enabled=scaler is not None):
                img = self.preprocess_train_sample(args, img)
                output = self.process_model_output(args, model(img))
                loss = criterion(output, target)

            optimizer.zero_grad()
            if scaler is not None:
                scaler.scale(loss).backward()
                if args.clip_grad_norm is not None:
                    scaler.unscale_(optimizer)
                    nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad_norm)
                scaler.step(optimizer)
                scaler.update()
            else:
                loss.backward()
                if args.clip_grad_norm is not None:
                    nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad_norm)
                optimizer.step()
            functional.reset_net(model)

            acc1, acc5 = self.cal_acc1_acc5(output, target)
            batch_size = target.shape[0]
            metric_logger.update(loss=loss.item(), lr=optimizer.param_groups[0]['lr'])
            metric_logger.meters['acc1'].update(acc1.item(), n=batch_size)
            metric_logger.meters['acc5'].update(acc5.item(), n=batch_size)
            metric_logger.meters['img/s'].update(batch_size / (time.time() - start_time))

        metric_logger.synchronize_between_processes()
        train_loss, train_acc1, train_acc5 = metric_logger.loss.global_avg, metric_logger.acc1.global_avg, metric_logger.acc5.global_avg
        print(f'Train: train_acc1={train_acc1:.3f}, train_acc5={train_acc5:.3f}, train_loss={train_loss:.6f}, samples/s={metric_logger.meters["img/s"]}')
        return train_loss, train_acc1, train_acc5

    def evaluate(self, args, model, criterion, data_loader, device, log_suffix=""):
        model.eval()
        metric_logger = utils.MetricLogger(delimiter=' ')
        header = f'Test: {log_suffix}'

        num_processed_samples = 0
        start_time = time.time()
        with torch.inference_mode():
            for img, target in metric_logger.log_every(data_loader, -1, header):
                img = img.to(device, non_blocking=True)
                target = F.one_hot(target, num_classes=args.num_classes).float().to(device, non_blocking=True)
                img = self.preprocess_test_sample(args, img)
                output = self.process_model_output(args, model(img))
                loss = criterion(output, target)

                acc1, acc5 = self.cal_acc1_acc5(output, target)

                batch_size = target.shape[0]
                metric_logger.update(loss=loss.item())
                metric_logger.meters['acc1'].update(acc1.item(), n=batch_size)
                metric_logger.meters['acc5'].update(acc5.item(), n=batch_size)
                num_processed_samples += batch_size
                functional.reset_net(model)

        num_processed_samples = utils.reduce_across_processes(num_processed_samples)
        if (
                hasattr(data_loader.dataset, '__len__')
                and len(data_loader.dataset) != num_processed_samples
                and utils.is_main_process()
        ):
            warnings.warn(
                f"It looks like the dataset has {len(data_loader.dataset)} samples, but {num_processed_samples} "
                "samples were used for the validation, which might bias the results. "
                "Try adjusting the batch size and / or the world size. "
                "Setting the world size to 1 is always a safe bet."
            )

        metric_logger.synchronize_between_processes()

        test_loss, test_acc1, test_acc5 = metric_logger.loss.global_avg, metric_logger.acc1.global_avg, metric_logger.acc5.global_avg
        print(f'Test: test_acc1={test_acc1:.3f}, test_acc5={test_acc5:.3f}, test_loss={test_loss:.6f}, samples/s={num_processed_samples / (time.time() - start_time):.3f}')
        return test_loss, test_acc1, test_acc5

    def preprocess_train_sample(self, args, x):
        x = x.unsqueeze(0).repeat(args.T, 1, 1, 1, 1)
        return self.encoder(x)

    def preprocess_test_sample(self, args, x):
        x = x.unsqueeze(0).repeat(args.T, 1, 1, 1, 1)
        return self.encoder(x)

    def process_model_output(self, args, y):
        return y.mean(0)

    def cal_acc1_acc5(self, output, target):
        acc1, acc5 = utils.accuracy(output, target, topk=(1, 5))
        return acc1, acc5

    def load_data(self, args):
        return self.load_CIFAR10(args)

    def load_model(self, args, num_classes):
        model_config = self.model_configs[args.model]
        model = MlpMixer(model_config, num_classes=num_classes, patch_size=model_config.patch_size)
        functional.set_step_mode(model, 'm')
        if args.cupy:
            functional.set_backend(model, 'cupy')
        return model

    def set_optimizer(self, args, parameters):
        opt_name = args.opt.lower()
        if opt_name == 'sgd':
            optimizer = torch.optim.SGD(
                parameters,
                lr=args.lr,
                momentum=args.momentum,
                weight_decay=args.weight_decay
            )
        elif opt_name == 'adamw':
            optimizer = torch.optim.AdamW(
                parameters,
                lr=args.lr,
                weight_decay=args.weight_decay
            )
        else:
            raise NotImplementedError(f'Not supported optimizer {args.opt}')
        return optimizer

    def set_lr_scheduler(self, args, optimizer):
        lr_scheduler = args.lr_scheduler.lower()
        if lr_scheduler == 'step':
            main_lr_scheduler = torch.optim.lr_scheduler.StepLR(
                optimizer,
                step_size=args.lr_step_size,
                gamma=args.lr_gamma
            )
        elif lr_scheduler == 'cosa':
            main_lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
                optimizer,
                T_max=args.epochs - args.lr_warmup_epochs
            )
        elif lr_scheduler == 'exp':
            main_lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(
                optimizer,
                gamma=args.lr_gamma
            )
        else:
            raise NotImplementedError(f'Not supported lr_scheduler {args.lr_scheduler}')
        if args.lr_warmup_epochs > 0:
            if args.lr_warmup_method == 'linear':
                warmup_lr_scheduler = torch.optim.lr_scheduler.LinearLR(
                    optimizer,
                    start_factor=args.lr_warmup_decay,
                    total_iters=args.lr_warmup_epochs
                )
            elif args.lr_warmup_method == 'constant':
                warmup_lr_scheduler = torch.optim.lr_scheduler.ConstantLR(
                    optimizer,
                    factor=args.lr_warmup_decay,
                    total_iters=args.lr_warmup_epochs
                )
            else:
                raise NotImplementedError(f'Not supported lr_warmup_method {args.lr_warmup_method}')
            lr_scheduler = torch.optim.lr_scheduler.SequentialLR(
                optimizer,
                schedulers=[warmup_lr_scheduler, main_lr_scheduler],
                milestones=[args.lr_warmup_epochs]
            )
        else:
            lr_scheduler = main_lr_scheduler

        return lr_scheduler

    def get_logdir_name(self, args):
        dir_name = f'{args.model}_b{args.batch_size}_e{args.epochs}' \
                   f'_{args.opt}_lr{args.lr}_wd{args.weight_decay}_seed{args.seed}'
        return dir_name

    def load_CIFAR10(self, args):
        print('Loading CIFAR10 Data...')
        dataset_train = torchvision.datasets.CIFAR10(
            root=args.data_path,
            download=True,
            train=True,
            transform=torchvision.transforms.Compose([
                torchvision.transforms.ToTensor(),
                torchvision.transforms.RandomResizedCrop(224),
                torchvision.transforms.Normalize(
                    mean=(0.4914, 0.4822, 0.4465),
                    std=(0.2023, 0.1994, 0.2010),
                )
            ]),
        )
        dataset_test = torchvision.datasets.CIFAR10(
            root=args.data_path,
            download=True,
            train=False,
            transform=torchvision.transforms.Compose([
                torchvision.transforms.ToTensor(),
                torchvision.transforms.Resize(224),
                torchvision.transforms.Normalize(
                    mean=(0.4914, 0.4822, 0.4465),
                    std=(0.2023, 0.1994, 0.2010),
                )
            ]),
        )

        loader_generator = torch.Generator()
        loader_generator.manual_seed(args.seed)

        if args.distributed:
            train_sampler = torch.utils.data.DistributedSampler(dataset=dataset_train, seed=args.seed)
            test_sampler = torch.utils.data.DistributedSampler(dataset=dataset_test, shuffle=False)
        else:
            train_sampler = torch.utils.data.RandomSampler(data_source=dataset_train, generator=loader_generator)
            test_sampler = torch.utils.data.SequentialSampler(data_source=dataset_test)

        return dataset_train, dataset_test, train_sampler, test_sampler

    def get_args_parser(self):
        parser = argparse.ArgumentParser()

        parser.add_argument('--data-path', default='./data', type=str)
        parser.add_argument('--model', default='mixer', type=str)
        parser.add_argument('--T', default=4, type=int)
        parser.add_argument('--cupy', action='store_true')
        parser.add_argument('--device', default='cuda', type=str)
        parser.add_argument('--batch-size', default=32, type=int)
        parser.add_argument('--epochs', default=90, type=int)
        parser.add_argument('--workers', default=16, type=int)
        parser.add_argument('--opt', default='sgd', type=str)
        parser.add_argument('--lr', default=0.1, type=float)
        parser.add_argument('--momentum', default=0.9, type=float)
        parser.add_argument('--weight-decay', default=0., type=float)
        parser.add_argument('--lr-scheduler', default='cosa', type=str)
        parser.add_argument('--lr-warmup-epochs', default=10, type=int)
        parser.add_argument('--lr-warmup-method', default='linear', type=str)
        parser.add_argument('--lr-warmup-decay', default=0.01, type=float)
        parser.add_argument('--lr-step-size', default=30, type=int)
        parser.add_argument('--lr-gamma', default=0.1, type=float)
        parser.add_argument('--output-dir', default='./logs', type=str)
        parser.add_argument('--resume', default=None, type=str)
        parser.add_argument('--start-epoch', default=0, type=int)
        parser.add_argument('--world-size', default=1, type=int)
        parser.add_argument('--dist-url', default='env://', type=str)
        parser.add_argument('--seed', default=42, type=int)
        parser.add_argument('--amp', action='store_true')
        parser.add_argument('--clip-grad-norm', default=None, type=float)
        parser.add_argument("--local-rank", type=int)
        parser.add_argument('--sync-bn', action='store_true')

        return parser


if __name__ == '__main__':
    trainer = Trainer()
    args = trainer.get_args_parser().parse_args()
    trainer.main(args)