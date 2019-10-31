#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author: penghuailiang
# @Date  : 2019/10/15


import torch
import torch.nn as nn
import torch.optim as optim
import util.logit as log
import utils
import ops
import os
import torch.nn.functional as F
from tqdm import tqdm
from dataset import FaceDataset
from tensorboardX import SummaryWriter
from module import ResidualBlock

"""
imitator
用来模拟游戏引擎：由params生成图片/灰度图
network: 8 layer
input: params (batch, 95)
output: tensor (batch, 3, 512, 512)
"""


class Imitator(nn.Module):
    def __init__(self, name, args, clean=True):
        """
        imitator
        :param name: imitator name
        :param args: argparse options
        """
        super(Imitator, self).__init__()
        self.name = name
        self.args = args
        self.initial_step = 0
        self.prev_path = "./output/preview"
        self.model_path = "./output/imitator"
        if clean:
            self.clean()
        self.writer = SummaryWriter(comment='imitator', log_dir=args.path_tensor_log)
        self.model = nn.Sequential(
            utils.deconv_layer(95, 512, kernel_size=4),  # 1. (batch, 512, 4, 4)
            ResidualBlock.make_layer(2, 512),  # enhance input signal
            utils.deconv_layer(512, 512, kernel_size=4, stride=2, pad=1),  # 2. (batch, 512, 8, 8)
            utils.deconv_layer(512, 512, kernel_size=4, stride=2, pad=1),  # 3. (batch, 512, 16, 16)
            utils.deconv_layer(512, 256, kernel_size=4, stride=2, pad=1),  # 4. (batch, 256, 32, 32)
            utils.deconv_layer(256, 128, kernel_size=4, stride=2, pad=1),  # 5. (batch, 128, 64, 64)
            utils.deconv_layer(128, 64, kernel_size=4, stride=2, pad=1),  # 6. (batch, 64, 128, 128)
            utils.deconv_layer(64, 64, kernel_size=4, stride=2, pad=1),  # 7. (batch, 64, 256, 256)
            nn.ConvTranspose2d(64, 3, kernel_size=4, stride=2, padding=1),  # 8. (batch, 3, 512, 512)
            nn.Sigmoid(),
            nn.Dropout(0.5),
        )
        self.model.apply(utils.init_weights)
        self.optimizer = optim.Adam(self.model.parameters(), lr=args.learning_rate)

    def forward(self, params):
        """
        construct network
        :param params: [batch, 95]
        :return: (batch, 1, 512, 512)
        """
        batch = params.size(0)
        length = params.size(1)
        _params = params.reshape((batch, length, 1, 1))
        _params = (_params * 2) - 1
        _params.requires_grad_(True)
        y = self.model(_params)
        return (y + 1) * 0.5

    def itr_train(self, params, reference):
        """
        iterator training
        :param params:  [batch, 95]
        :param reference: reference photo [batch, 1, 512, 512]
        :return loss: [batch], y_: generated picture
        """
        self.optimizer.zero_grad()
        y_ = self.forward(params)
        loss = F.l1_loss(reference, y_)
        loss.backward()  # 求导  loss: [1] scalar
        self.optimizer.step()  # 更新网络参数权重
        return loss, y_

    def batch_train(self, cuda=False):
        """
        batch training
        :param cuda: 是否开启gpu加速运算
        """
        rnd_input = torch.randn(self.args.batch_size, self.args.params_cnt)
        if cuda:
            rnd_input = rnd_input.cuda()
        self.writer.add_graph(self, input_to_model=rnd_input)

        self.model.train()
        dataset = FaceDataset(self.args, mode="train")
        initial_step = self.initial_step
        total_steps = self.args.total_steps
        progress = tqdm(range(initial_step, total_steps + 1), initial=initial_step, total=total_steps)
        for step in progress:
            names, params, images = dataset.get_batch(batch_size=self.args.batch_size, edge=False)
            if cuda:
                params = params.cuda()
                images = images.cuda()

            loss, y_ = self.itr_train(params, images)
            loss_ = loss.cpu().detach().numpy()
            progress.set_description("loss: {:.3f}".format(loss_))
            self.writer.add_scalar('imitator/loss', loss_, step)

            if (step + 1) % self.args.prev_freq == 0:
                path = "{1}/imit_{0}.jpg".format(step + 1, self.prev_path)
                ops.save_img(path, images, y_)
                lr = self.args.learning_rate * (total_steps - step) / float(total_steps) + 1e-6
                utils.update_optimizer_lr(self.optimizer, lr)
                self.writer.add_scalar('imitator/learning rate', lr, step)
                self.upload_weights(step)
            if (step + 1) % self.args.save_freq == 0:
                self.save(step)
        self.writer.close()

    def upload_weights(self, step):
        """
        把neural net的权重以图片的方式上传到tensorboard
        :param step: train step
        :return weights picture
        """
        for module in self.model._modules.values():
            if isinstance(module, nn.Sequential):
                for it in module._modules.values():
                    if isinstance(it, nn.ConvTranspose2d):
                        if it.in_channels == 32 and it.out_channels == 32:
                            name = "weight_{0}_{1}".format(it.in_channels, it.out_channels)
                            # log.info(it.weight.shape)
                            weights = it.weight.reshape(4, 64, -1)
                            self.writer.add_image(name, weights, step)
                            return weights

    def load_checkpoint(self, path, training=False, cuda=False):
        """
        从checkpoint 中恢复net
        :param training: 恢复之后 是否接着train
        :param path: checkpoint's path
        :param cuda: gpu speedup
        """
        checkpoint = torch.load(self.args.path_to_inference + "/" + path)
        self.model.load_state_dict(checkpoint['net'])
        self.optimizer.load_state_dict(checkpoint['optimizer'])
        self.initial_step = checkpoint['epoch']
        log.info("recovery imitator from %s", path)
        if training:
            self.batch_train(cuda)

    def inference(self, path, params, cuda=False):
        """
        imitator生成图片
        :param path: checkpoint's path
        :param params: engine's params
        :param cuda: gpu speedup
        :return: images [batch, 1, 512, 512]
        """
        self.load_checkpoint(path, cuda=cuda)
        _, images = self.forward(params)
        return images

    def evaluate(self):
        """
        评估准确率
        :return: accuracy rate
        """
        self.model.eval()
        dataset = FaceDataset(self.args, mode="test")
        steps = 100
        accuracy = 0.0
        location = self.args.lightcnn
        for step in range(steps):
            log.info("step: %d", step)
            names, params, images = dataset.get_batch(batch_size=self.args.batch_size, edge=False)
            loss, _ = self.itr_train(params, images)
            accuracy += 1.0 - loss
        accuracy = accuracy / steps
        log.info("accuracy rate is %f", accuracy)
        return accuracy

    def clean(self):
        """
        清空前记得手动备份
        :return:
        """
        ops.clear_files(self.args.path_tensor_log)
        ops.clear_files(self.prev_path)
        ops.clear_files(self.model_path)

    def save(self, step):
        """
       save checkpoint
       :param step: train step
       """
        state = {'net': self.model.state_dict(), 'optimizer': self.optimizer.state_dict(), 'epoch': step}
        if not os.path.exists(self.model_path):
            os.mkdir(self.model_path)
        torch.save(state, '{1}/model_imitator_{0}.pth'.format(step + 1, self.model_path))
