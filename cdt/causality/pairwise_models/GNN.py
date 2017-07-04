"""
GNN : Generative Neural Networks for causal inference (pairwise)
Authors : Olivier Goudet & Diviyan Kalainathan
Ref:
Date : 10/05/2017
"""

import tensorflow as tf
import numpy as np
from ...utils.loss import MMD_loss_th as MMD_th
from ...utils.loss import MMD_loss_tf as MMD_tf
from ...utils.SETTINGS import CGNN_SETTINGS as SETTINGS
from joblib import Parallel, delayed
from sklearn.preprocessing import scale
import torch as th
from torch.autograd import Variable
from .model import Pairwise_Model


def init(size, **kwargs):
    """ Initialize a random tensor, normal(0,kwargs(SETTINGS.init_weights)).

    :param size: Size of the tensor
    :param kwargs: init_std=(SETTINGS.init_weights) Std of the initialized normal variable
    :return: Tensor
    """
    init_std = kwargs.get('init_std', SETTINGS.init_weights)
    return tf.random_normal(shape=size, stddev=init_std)

class GNN_tf(object):
    def __init__(self, N, run=0, pair=0, **kwargs):
        """ Build the tensorflow graph, the first column is set as the cause and the second as the effect

        :param N: Number of examples to generate
        :param run: for log purposes (optional)
        :param pair: for log purposes (optional)
        :param kwargs: h_layer_dim=(SETTINGS.h_dim) Number of units in the hidden layer
        :param kwargs: learning_rate=(SETTINGS.learning_rate) learning rate of the optimizer
        """

        h_layer_dim = kwargs.get('h_layer_dim', SETTINGS.h_dim)
        learning_rate = kwargs.get('learning_rate', SETTINGS.learning_rate)
        self.run = run
        self.pair = pair
        self.X = tf.placeholder(tf.float32, shape=[None, 1])
        self.Y = tf.placeholder(tf.float32, shape=[None, 1])

        W_in = tf.Variable(init([2, h_layer_dim], **kwargs))
        b_in = tf.Variable(init([h_layer_dim], **kwargs))
        W_out = tf.Variable(init([h_layer_dim, 1], **kwargs))
        b_out = tf.Variable(init([1], **kwargs))

        theta_G = [W_in, b_in,
                   W_out, b_out]

        e = tf.random_normal([N, 1], mean=0, stddev=1)
        input = tf.concat([self.X, e], 1)
        hid = tf.nn.relu(tf.matmul(input, W_in) + b_in)
        out = tf.matmul(hid, W_out) + b_out

        self.G_dist_loss_xcausesy = MMD_tf(tf.concat([self.X, self.Y], 1), tf.concat([self.X, out], 1))
        self.G_solver_xcausesy = (tf.train.AdamOptimizer(learning_rate=learning_rate)
                                  .minimize(self.G_dist_loss_xcausesy, var_list=theta_G))

        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        self.sess = tf.Session(config=config)
        self.sess.run(tf.global_variables_initializer())

    def train(self, data, verbose=True, **kwargs):
        """ Train the GNN model

        :param data: data corresponding to the graph
        :param verbose: verbose
        :param kwargs: train_epochs=(SETTINGS.nb_epoch_train) number of train epochs
        :return: None
        """
        train_epochs = kwargs.get('train_epochs', SETTINGS.nb_epoch_train)

        for it in range(train_epochs):
            _, G_dist_loss_xcausesy_curr = self.sess.run(
                [self.G_solver_xcausesy, self.G_dist_loss_xcausesy],
                feed_dict={self.X: data[:, [0]], self.Y: data[:, [1]]}
            )

            if verbose:
                if (it % 100 == 0):
                    print('Pair:{}, Run:{}, Iter:{}, score:{}'.
                          format(self.pair, self.run,
                                 it, G_dist_loss_xcausesy_curr))

    def evaluate(self, data, verbose=True, **kwargs):
        """ Test the model

        :param data: data corresponding to the graph
        :param verbose: verbose
        :param kwargs: test_epochs=(SETTINGS.nb_epoch_test) number of test epochs
        :return: mean MMD loss value of the CGNN structure on the data
        """
        test_epochs = kwargs.get('test_epochs', SETTINGS.nb_epoch_test)
        avg_score = 0

        for it in range(test_epochs):
            score = self.sess.run([self.G_dist_loss_xcausesy], feed_dict={self.X: data[:, [0]], self.Y: data[:, [1]]})

            avg_score += score[0]

            if verbose:
                if (it % 100 == 0):
                    print('Pair:{}, Run:{}, Iter:{}, score:{}'.format(self.pair, self.run, it, score[0]))

        tf.reset_default_graph()

        return avg_score / test_epochs


def tf_evalcausalscore_pairwise(df, idx, run, **kwargs):
    GNN = GNN_tf(df.shape[0], run, idx, **kwargs)
    GNN.train(df, **kwargs)
    return GNN.evaluate(df, **kwargs)


def tf_run_pair(m, idx, run, **kwargs):
    """ Execute the CGNN, by init, train and eval either on CPU or GPU

    :param m: data corresponding to the config : (N, 2) data, [:, 0] cause and [:, 1] effect
    :param run: number of the run (only for print)
    :param idx: number of the idx (only for print)
    :param kwargs: gpu=(SETTINGS.GPU) True if GPU is used
    :param kwargs: num_gpu=(SETTINGS.num_gpu) Number of available GPUs
    :param kwargs: gpu_offset=(SETTINGS.gpu_offset) number of gpu offsets
    :return: MMD loss value of the given structure after training
    """
    gpu = kwargs.get('gpu', SETTINGS.GPU)
    num_gpu = kwargs.get('num_gpu', SETTINGS.num_gpu)
    gpu_offset = kwargs.get('gpu_offset', SETTINGS.gpu_offset)

    run_i = run
    if gpu:
        with tf.device('/gpu:' + str(gpu_offset + run_i % num_gpu)):
            XY = tf_evalcausalscore_pairwise(m, idx, run, **kwargs)
        with tf.device('/gpu:' + str(gpu_offset + run_i % num_gpu)):
            YX = tf_evalcausalscore_pairwise(m[:, [1, 0]], idx, run, **kwargs)
            return [XY, YX]
    else:
        return [tf_evalcausalscore_pairwise(m, idx, run, **kwargs),
                tf_evalcausalscore_pairwise(np.fliplr(m), idx, run, **kwargs)]


def predict_tf(a, b, **kwargs):
    """
    :param a: Cause of the pair
    :param b: Effect of the pair
    :param kwargs: nb_jobs=(SETTINGS.nb_jobs) number of jobs
    :param kwargs: nb_runs=(SETTINGS.nb_runs) number of runs, of different evaluations
    :return: evaluation of the pair
    """
    nb_jobs = kwargs.get("nb_jobs", SETTINGS.nb_jobs)
    nb_run = kwargs.get("nb_run", SETTINGS.nb_run)
    m = np.hstack((a, b))
    m = scale(m)
    m = m.astype('float32')

    result_pair = Parallel(n_jobs=nb_jobs)(delayed(tf_run_pair)(
        m, 0, run, **kwargs) for run in range(nb_run))

    score_AB = np.mean([runpair[0] for runpair in result_pair])
    score_BA = np.mean([runpair[1] for runpair in result_pair])

    return (score_BA - score_AB) / (score_BA + score_AB)


class GNN_th(th.nn.Module):
    def __init__(self, **kwargs):
        """
        Build the Torch graph
        :param kwargs: h_layer_dim=(SETTINGS.h_dim) Number of units in the hidden layer
        """
        super(GNN_th, self).__init__()
        h_layer_dim = kwargs.get('h_layer_dim', SETTINGS.h_dim)

        self.l1 = th.nn.Linear(2, h_layer_dim)
        self.l2 = th.nn.Linear(h_layer_dim, 1)
        self.act = th.nn.ReLU()
        # ToDo : Init parameters

    def forward(self, x):
        """
        Pass data through the net structure
        :param x: input data: shape (:,2)
        :type x: torch.Variable
        :return: output of the shallow net
        :rtype: torch.Variable

        """
        x = self.act(self.l1(x))
        return self.l2(x)


def run_GNN_th(m, pair, run, **kwargs):
    """ Train and eval the GNN on a pair

    :param m: Matrix containing cause at m[:,0],
              and effect at m[:,1]
    :type m: numpy.ndarray
    :param pair: Number of the pair
    :param run: Number of the run
    :param kwargs: gpu=(SETTINGS.GPU) True if GPU is used
    :param kwargs: train_epochs=(SETTINGS.nb_epoch_train) number of train epochs
    :param kwargs: test_epochs=(SETTINGS.nb_epoch_test) number of test epochs
    :param kwargs: learning_rate=(SETTINGS.learning_rate) learning rate of the optimizer
    :return: Value of the evaluation after training
    :rtype: float
    """
    gpu = kwargs.get('gpu', SETTINGS.GPU)
    train_epochs = kwargs.get('test_epochs', SETTINGS.nb_epoch_train)
    test_epochs = kwargs.get('test_epochs', SETTINGS.nb_epoch_test)
    learning_rate = kwargs.get('learning_rate', SETTINGS.learning_rate)

    x = Variable(th.from_numpy(m[:, [0]]))
    y = Variable(th.from_numpy(m[:, [1]]))
    e = Variable(th.FloatTensor(m.shape[0], 1))
    GNN = GNN_th(**kwargs)

    if gpu:
        x = x.cuda()
        y = y.cuda()
        e = e.cuda()
        GNN = GNN.cuda()

    criterion = MMD_th(m.shape[0], cuda=gpu)

    optim = th.optim.Adam(GNN.parameters(), lr=learning_rate)
    running_loss = 0
    teloss = 0

    for i in range(train_epochs):
        optim.zero_grad()
        e.data.normal_()
        x_in = th.cat([x, e], 1)
        y_pred = GNN(x_in)
        loss = criterion(x, y_pred, y)
        loss.backward()
        optim.step()

        # print statistics
        running_loss += loss.data[0]
        if i % 300 == 299:  # print every 2000 mini-batches
            print('Pair:{}, Run:{}, Iter:{}, score:{}'.
                  format(pair, run, i, running_loss))
            running_loss = 0.0

    # Evaluate
    for i in range(test_epochs):
        e.data.normal_()
        x_in = th.cat([x, e], 1)
        y_pred = GNN(x_in)
        loss = criterion(x, y_pred, y)

        # print statistics
        running_loss += loss.data[0]
        teloss += running_loss
        if i % 300 == 299:  # print every 300 batches
            print('Pair:{}, Run:{}, Iter:{}, score:{}'.
                  format(pair, run, i, running_loss))
            running_loss = 0.0

    return teloss / test_epochs


def th_run_instance(m, pair_idx=0, run=0, **kwargs):
    """

    :param m: data corresponding to the config : (N, 2) data, [:, 0] cause and [:, 1] effect
    :param pair_idx: print purposes
    :param run: numner of the run (for GPU dispatch)
    :param kwargs: gpu=(SETTINGS.GPU) True if GPU is used
    :param kwargs: num_gpu=(SETTINGS.num_gpu) Number of available GPUs
    :param kwargs: gpu_offset=(SETTINGS.gpu_offset) number of gpu offsets
    :return:
    """
    gpu = kwargs.get('gpu', SETTINGS.GPU)
    num_gpu = kwargs.get('num_gpu', SETTINGS.num_gpu)
    gpu_offset = kwargs.get('gpu_offset', SETTINGS.gpu_offset)

    if gpu:
        with th.cuda.device(gpu_offset + run % num_gpu):
            XY = run_GNN_th(m, pair_idx, run, **kwargs)
        with th.cuda.device(gpu_offset + run % num_gpu):
            YX = run_GNN_th(np.fliplr(m), pair_idx, run, **kwargs)

    else:
        XY = run_GNN_th(m, pair_idx, run, **kwargs)
        YX = run_GNN_th(m, pair_idx, run, **kwargs)

    return [XY, YX]


def predict_th(a, b):
    m = np.hstack((a, b))
    m = scale(m)
    m = m.astype('float32')
    result_pair = Parallel(n_jobs=SETTINGS.nb_jobs)(delayed(th_run_instance)(
        m, 0, run) for run in range(SETTINGS.nb_run))

    score_XY = np.mean([runpair[0] for runpair in result_pair])
    score_YX = np.mean([runpair[1] for runpair in result_pair])
    return (score_YX - score_XY) / (score_YX + score_XY)


class GNN(Pairwise_Model):
    """
    Shallow Generative Neural networks, models the causal directions x->y and y->x with a 1-hidden layer neural network
    and a MMD loss. The causal direction is considered as the "best-fit" between the two directions
    """

    def __init__(self, backend="PyTorch"):
        super(GNN, self).__init__()
        self.backend = backend

    def predict_proba(self, a, b):
        if len(np.array(a).shape) == 1:
            a = np.array(a).reshape((-1, 1))
            b = np.array(b).reshape((-1, 1))

        if self.backend == "PyTorch":
            return predict_th(a, b)
        elif self.backend == "TensorFlow":
            return predict_tf(a, b)
        else:
            print('No backend known as {}'.format(self.backend))
            raise ValueError
