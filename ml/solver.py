#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
@Time    : 2019-12-24 21:47
@Author  : Wang Xin
@Email   : wangxin_buaa@163.com
@File    : solver.py
"""
import gc
import logging
import sys

from tqdm import tqdm

from config import ConfigNameSpace
from data.datasets import get_dataloader
from ml.metrics.average_meter import AverageMeter
from ml.metrics.metrics import Metrics
from ml.models import get_model
from ml.models.base_model import BaseModel
from ml.optimizers import get_optimizer, get_lr_policy
from ml.visualizers.basic_visualizer import Visualizer

logging.basicConfig(format='[%(asctime)s %(levelname)s] %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)

import os
import time

import numpy as np
import random
import inspect

import torch
from ml.utils.pyt_io import load_model, create_summary_writer
from ml.utils.pyt_ops import tensor2cuda

# try:
#     from apex import amp
#     from apex.parallel import convert_syncbn_model, DistributedDataParallel
# except ImportError:
#     raise ImportError(
#         "Please install apex from https://www.github.com/nvidia/apex .")


RANDOM_SEED = 5


class Solver(object):

    def __init__(self, args, config=None):
        """
            :param config: easydict
        """
        self.epoch = 0
        self.iteration = 0
        self.config = None
        self.result_dir = None
        self.model, self.optimizer, self.lr_policy = None, None, None
        self.writer = None
        self.model_input_keys = None
        self.best_rmses = []

        self.args = args

        self.set_config(config)

        # get dataloaders
        self.train_loader, self.niter_train = get_dataloader(self.config.data, is_train=True)
        self.val_loader, self.niter_val = get_dataloader(self.config.data, is_train=False)

        self.loss_meter = AverageMeter()
        self.train_metric = Metrics(self.result_dir, tag='train', niter=self.niter_train)
        self.val_metric = Metrics(self.result_dir, tag='val', niter=self.niter_val)
        self.visualizer = Visualizer(self.writer)

    def set_config(self, config_file=None):
        """
        Make the config namespace according to the arguments and the config file path. If args.mode is resumed or eval then load it from the model file (according to the id argument), othervise load from the config file.

        :param config_file: path of the config file
        """
        # read config from model
        if self.args.mode in ['eval', 'resume', 'test']:
            # TODO
            raise NotImplementedError('TODO')
            # device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            continue_state_object = torch.load(self.args.resumed,
                                               map_location=torch.device("cpu"))
            self.config = continue_state_object['config']
            self.result_dir = self.args.resumed[:-len(self.args.resumed.split('/')[-1])]
            if not os.path.exists(self.result_dir):
                logging.error('[Error] {} is not existed.'.format(self.result_dir))
                raise FileNotFoundError
            self.writer = create_summary_writer(self.result_dir)
            self.init_from_checkpoint(continue_state_object=continue_state_object)

        # read config from config file
        elif self.args.mode in ['pretrain', 'train']:
            self.config = ConfigNameSpace(self.args.config)
            self.config.id = self.args.id

            # if no id is provided, then use the current timestamp
            if self.config.id is not None:
                exp_name = self.args.id
            else:
                exp_name = time.strftime('%Y_%m_%d-%H_%M_%S', time.localtime())

            # ad _pretrain after name to not mix up if the mode is pretrain
            if self.args.mode == 'pretrain':
                exp_name = exp_name + '_pretrain'

            # path for the results
            self.result_dir = os.path.join('../results/DepthPrediction', exp_name)
            if not os.path.exists(self.result_dir):
                os.makedirs(self.result_dir)

            # save config
            self.config.save(os.path.join(self.result_dir, 'config.yaml'))

            # tensorboard writer
            self.tensorboard_dir = os.path.join(self.result_dir, 'tensorboard')
            if not os.path.exists(self.tensorboard_dir):
                os.makedirs(self.tensorboard_dir)
            self.writer = create_summary_writer(self.tensorboard_dir)

            # model dir
            self.models_dir = os.path.join(self.result_dir, 'models')
            if not os.path.exists(self.models_dir):
                os.makedirs(self.models_dir)

            # initialize the solver
            self.init_from_scratch()
        else:
            raise ValueError(f'Wrong mode argument: {self.args.mode}.')

    def _set_seed(self):
        """
        Set the random seeds
        """
        torch.manual_seed(RANDOM_SEED)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(RANDOM_SEED)
        np.random.seed(RANDOM_SEED)
        random.seed(RANDOM_SEED)

    def init_from_scratch(self):
        """
        Init solver args from scratch.
        """
        t_start = time.time()
        self._set_seed()

        # model and optimizer
        self.model = get_model(self.config.model)
        self.model_input_keys = [p.name for p in inspect.signature(self.model.forward).parameters.values()]

        model_params = self.model.parameters()
        self.optimizer = get_optimizer(self.config.optimizer, model_params=model_params)
        self.lr_policy = get_lr_policy(self.config.lr_policy, optimizer=self.optimizer)

        self.model.cuda()

        t_end = time.time()
        logging.info("Init trainer from scratch, Time usage: {}".format(t_end - t_start))

    def init_from_checkpoint(self, continue_state_object):
        t_start = time.time()

        self.config = continue_state_object['config']
        self._set_seed()
        self.model = BaseModel(self.config['model']['params'])
        self.model_input_keys = [p.name for p in inspect.signature(self.model).parameters.values()]

        self.model_input_keys.append('target')

        # model_params = filter(lambda p: p.requires_grad, self.model.parameters())

        model_params = self.model.parameters()
        self.optimizer = _get_dorn_optimizer(self.config['solver']['optimizer'], model_params=model_params)
        self.lr_policy = _get_lr_policy(self.config['solver']['lr_policy'], optimizer=self.optimizer)

        load_model(self.model, continue_state_object['model'], distributed=False)
        self.model.cuda()

        self.optimizer.load_state_dict(continue_state_object['optimizer'])
        self.lr_policy.load_state_dict(continue_state_object['lr_policy'])

        self.epoch = continue_state_object['epoch']
        self.iteration = continue_state_object["iteration"]

        del continue_state_object
        t_end = time.time()
        logging.info("Init trainer from checkpoint, Time usage: {}".format(t_end - t_start))

    def get_model_input_dict(self, minibatch):
        model_input_dict = {k: v for k, v in minibatch.items() if k in self.model_input_keys}
        if torch.cuda.is_available():
            model_input_dict = tensor2cuda(model_input_dict)
        else:
            raise SystemError('No cuda device found.')
        return model_input_dict

    def get_loader(self, mode, scenes=None):
        if mode == 'train':
            return self.train_loader, self.niter_train
        elif mode == 'val':
            return self.val_loader, self.niter_val
        elif mode == 'scene_retrain':
            scene_avg = np.median([val for key, val in scenes.items()])
            retrain_scenes = [key for key, val in scenes.items() if val > scene_avg]
            return get_dataloader(self.config, is_train=True, scenes=retrain_scenes)
        else:
            raise ValueError(f'Wrong solver mode: {mode}')

    def get_metric(self, mode, niter=None):
        if mode == 'train':
            return self.train_metric
        elif mode == 'val':
            return self.val_metric
        elif mode == 'scene_retrain':
            return Metrics(self.result_dir, tag='bad_scene', niter=niter)
        else:
            raise ValueError(f'Wrong solver mode: {mode}')

    def run_epoch(self, mode='train', scenes=None):
        loader, niter = self.get_loader(mode, scenes=scenes)
        metric = self.get_metric(mode, niter=niter)
        epoch_iterator = iter(loader)

        # set loading bar
        bar_format = '{desc}[{elapsed}<{remaining},{rate_fmt}]'
        pbar = tqdm(range(niter), file=sys.stdout, bar_format=bar_format)

        for idx in pbar:
            # start measuring preproc time
            t_start = time.time()

            # get the minibatch and filter out to the input and gt elements
            minibatch = epoch_iterator.next()
            model_input_dict = self.get_model_input_dict(minibatch)

            # preproc time
            t_end = time.time()
            preproc_time = t_end - t_start

            # start measuring the train time
            t_start = time.time()

            # train
            pred, loss = self.step(mode, **model_input_dict)

            # train time
            t_end = time.time()
            cmp_time = t_end - t_start
            if loss is not None and mode == 'train':
                self.loss_meter.update(loss)
                self.writer.add_scalar("train/loss", self.loss_meter.mean(), self.epoch)
            else:
                loss = torch.as_tensor(0)
            metric.compute_metric(pred, model_input_dict, minibatch['scene'])

            print_str = f'[{mode}] Epoch {self.epoch}/{self.config.env.epochs} ' \
                        + f'Iter{idx + 1}/{niter}: ' \
                        + f'lr={self.get_learning_rates()[0]:.8f} ' \
                        + f'losses={loss.item():.2f}({self.loss_meter.mean():.2f}) ' \
                        + metric.get_snapshot_info() \
                        + f' prp: {preproc_time:.2f}s ' \
                        + f'inf :{cmp_time:.2f}s ' \

            pbar.set_description(print_str, refresh=False)

            if idx % self.config.env.save_train_frequency == 0:
                self.visualizer.visualize(minibatch, pred, self.epoch, tag='train')
                metric.add_scalar(self.writer, iteration=idx)

    def train(self):
        # start epoch
        for self.epoch in range(self.epoch, self.config.env.epochs):
            self.before_epoch()

            self.run_epoch(mode='train')

            # run another epoch on scenes with bad results
            if self.config.env.bad_scene_retrain:
                self.bad_scene_retrain(self.train_metric.rmse_scene_meter.mean())

            self.after_epoch()
            self.train_metric.on_epoch_end()
            # validation
            # first set the val dataset lidar sparsity to the train data current one
            # self.val_loader.dataset.lidar_sparsity = self.train_loader.dataset.lidar_sparsity
            self.eval()
            self.save_best_checkpoint(self.val_metric.epoch_results)


        self.writer.close()

        return min(self.train_metric.epoch_results['rmse'])  # best value

    def bad_scene_retrain(self, scenes):
        self.run_epoch(mode='scene_retrain', scenes=scenes)

    def eval(self):
        self.run_epoch(mode='val')

        logging.info(f'After Epoch {self.epoch}/{self.config.env.epochs}, {self.val_metric.get_result_info()}')

        self.val_metric.on_epoch_end()

    def step(self, mode='train', **model_inputs):
        """
        :param model_inputs:
        :return:
        """
        if mode == 'val':
            with torch.no_grad():
                pred = self.model(**model_inputs)

            return pred['pred'], None

        elif mode == 'train' or mode == 'scene_retrain':
            self.iteration += 1
            output_dict = self.model(**model_inputs)

            pred = output_dict['pred']
            loss = output_dict['loss']

            # backward
            loss.backward()

            self.optimizer.step()
            self.optimizer.zero_grad()
            self.lr_policy.step(self.epoch)

            return pred, loss.data

    def before_epoch(self):
        self.iteration = 0
        self.epoch = self.epoch
        self.model.train()
        torch.cuda.empty_cache()

    def after_epoch(self):
        self.model.eval()
        gc.collect()
        torch.cuda.empty_cache()

    def save_best_checkpoint(self, epoch_results):
        if not min(epoch_results['irmse']) == epoch_results['irmse'][-1]:
            return

        path = os.path.join(self.result_dir, 'model_best.pth')

        t_start = time.time()

        state_dict = {}

        from collections import OrderedDict
        new_state_dict = OrderedDict()
        for k, v in self.model.state_dict().items():
            key = k
            if k.split('.')[0] == 'module':
                key = k[7:]
            new_state_dict[key] = v

        state_dict['config'] = self.config
        state_dict['model'] = new_state_dict
        state_dict['optimizer'] = self.optimizer.state_dict()
        state_dict['lr_policy'] = self.lr_policy.state_dict()
        state_dict['epoch'] = self.epoch
        state_dict['iteration'] = self.iteration

        t_iobegin = time.time()
        torch.save(state_dict, path)
        del state_dict
        del new_state_dict
        t_end = time.time()
        logging.info(
            "Save checkpoint to file {}, "
            "Time usage:\n\tprepare snapshot: {}, IO: {}".format(
                path, t_iobegin - t_start, t_end - t_iobegin))

    def get_learning_rates(self):
        lrs = []
        for i in range(len(self.optimizer.param_groups)):
            lrs.append(self.optimizer.param_groups[i]['lr'])
        return lrs
