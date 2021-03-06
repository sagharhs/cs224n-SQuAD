from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import logging
import os
from datetime import datetime
#import pgb

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf
from tensorflow.python.ops import variable_scope as vs
from tensorflow.python.ops.nn import dynamic_rnn, bidirectional_dynamic_rnn

from evaluate import exact_match_score, f1_score
from util import ConfusionMatrix, Progbar, minibatches, get_minibatches, one_hot
from defs import LBLS

from q2_rnn_cell import RNNCell

logging.basicConfig(level=logging.INFO)


def get_optimizer(opt):
    if opt == "adam":
        optfn = tf.train.AdamOptimizer
    elif opt == "sgd":
        optfn = tf.train.GradientDescentOptimizer
    else:
        assert (False)
    return optfn




class LSTMAttnCell(tf.nn.rnn_cell.LSTMCell):
    def __init__(self, num_units, encoder_output, scope=None):
        self.hs = encoder_output
        super(LSTMAttnCell,self).__init__(num_units)


    def __call__(self, inputs, state, scope=None):
        lstm_out, lstm_state = super(LSTMAttnCell,self).__call__(inputs, state, scope)
        with vs.variable_scope(scope or type(self).__name__):
            with vs.variable_scope("Attn"):
                ht = tf.nn.rnn_cell._linear(lstm_out, self._num_units, True, 1.0)
                ht = tf.expand_dims(ht, axis=1)
            scores = tf.reduce_sum(self.hs*ht, reduction_indices=2, keep_dims=True)
            scores = tf.exp(scores - tf.reduce_max(scores, reduction_indices=1, keep_dims=True))
            scores = scores / (1e-6 + tf.reduce_sum(scores, reduction_indices=1, keep_dims=True))
            context = tf.reduce_sum(self.hs*scores, reduction_indices=1)
            with vs.variable_scope("AttnConcat"):
                out = tf.nn.relu(tf.nn.rnn_cell._linear([context, lstm_out], self._num_units, True, 1.0))

        return (out, tf.nn.rnn_cell.LSTMStateTuple(out,out))

class Encoder(object):
    def __init__(self, size, vocab_dim):
        self.size = size
        self.vocab_dim = vocab_dim

    def length(self, mask):
        used = tf.cast(mask, tf.int32)
        length = tf.reduce_sum(used, reduction_indices=1)
        length = tf.cast(length, tf.int32)
        return length


    def encode_questions(self, inputs, masks, encoder_state_input):
        """
        In a generalized encode function, you pass in your inputs,
        masks, and an initial
        hidden state input into this function.
        :param inputs: Symbolic representations of your input with shape = (batch_size, length/max_length, embed_size)
        :param masks: this is to make sure tf.nn.dynamic_rnn doesn't iterate
                      through masked steps
        :param encoder_state_input: (Optional) pass this as initial hidden state
                                    to tf.nn.dynamic_rnn to build conditional representations
        :return: an encoded representation of your input.
                 It can be context-level representation, word-level representation,
                 or both.
        """
        if encoder_state_input == None:
            encoder_state_input = tf.zeros([1, self.size])
        cell_size = self.size
        #initial_state_fw_cell = tf.slice(encoder_state_input, [0,0],[-1,cell_size])
        #initial_state_bw_cell = tf.slice(encoder_state_input, [0,cell_size],[-1,cell_size])
        #cell_fw = tf.nn.rnn_cell.LSTMCell(num_units=cell_size, state_is_tuple=True)
        #cell_bw = tf.nn.rnn_cell.LSTMCell(num_units=cell_size, state_is_tuple=True)
        cell_fw = tf.nn.rnn_cell.BasicLSTMCell(self.size)
        cell_bw = tf.nn.rnn_cell.BasicLSTMCell(self.size)

        with tf.variable_scope("bi_LSTM"):
            outputs, final_state = tf.nn.bidirectional_dynamic_rnn(
                                            cell_fw,
                                            cell_bw,
                                            dtype=tf.float32,
                                            sequence_length=self.length(masks),
                                            inputs= inputs,
                                            time_major = False
                                            )

        final_state_fw = final_state[0].h
        final_state_bw = final_state[1].h
        final_state = tf.concat(1, [final_state_fw, final_state_bw])
        states = tf.concat(2, outputs)
        return final_state, states

    def encode_w_attn(self, inputs, masks, prev_states, scope="", reuse=False):
        """
        Run a BiLSTM over the context paragraph conditioned on the question representation.
        """
        cell_size = self.size
        prev_states_fw, prev_states_bw = tf.split(2, 2, prev_states)
        attn_cell_fw = LSTMAttnCell(cell_size, prev_states_fw)
        attn_cell_bw = LSTMAttnCell(cell_size, prev_states_bw)
        with vs.variable_scope(scope, reuse):
            outputs, final_state = tf.nn.bidirectional_dynamic_rnn(
                                            attn_cell_fw,
                                            attn_cell_bw,
                                            dtype=tf.float32,
                                            sequence_length=self.length(masks),
                                            inputs= inputs,
                                            time_major = False
                                            )
        final_state_fw = final_state[0].h
        final_state_bw = final_state[1].h
        final_state = tf.concat(1, [final_state_fw, final_state_bw])
        states = tf.concat(2, outputs)
        return final_state, states



class Decoder(object):
    def __init__(self, output_size):
        self.output_size = 2*output_size

    def match_LASTM(self,questions_states, paragraph_states, question_length, paragraph_length, drop_out_rate):

        fw_states = []
        with tf.variable_scope("Forward_Match-LSTM"):
            cell = tf.nn.rnn_cell.LSTMCell(self.output_size, initializer=tf.contrib.layers.xavier_initializer())
            W_q = tf.get_variable("W_q", shape=(self.output_size, self.output_size), initializer=tf.contrib.layers.xavier_initializer())
            W_r = tf.get_variable("W_r", shape=(self.output_size, self.output_size), initializer=tf.contrib.layers.xavier_initializer())
            b_p = tf.get_variable("b_p", shape=(self.output_size), initializer=tf.contrib.layers.xavier_initializer())
            w = tf.get_variable("w", shape=(self.output_size,1), initializer=tf.contrib.layers.xavier_initializer())
            b = tf.get_variable("b", shape=(1,1), initializer=tf.contrib.layers.xavier_initializer())
            state = None
            c = None
            for time_step in range(paragraph_length):
                p_state = paragraph_states[:,time_step,:]
                X_ = tf.reshape(questions_states, [-1, self.output_size])
                if state is not None:
                    G = tf.nn.tanh(tf.matmul(X_,W_q) + tf.matmul(p_state,W_r) + tf.matmul(state,W_r)+b_p) #batch_size*Q,l
                else:
                    G = tf.nn.tanh(
                        tf.matmul(X_, W_q) + tf.matmul(p_state, W_r) + b_p)  # batch_size*Q,l
                atten = tf.nn.softmax(tf.matmul(G, w) + b) #batch_size*Q,1
                atten = tf.reshape(atten, [-1, 1, question_length])
                X_ = tf.reshape(questions_states, [-1, question_length, self.output_size])
                p_z = tf.matmul(atten, X_)
                p_z = tf.reshape(p_z, [-1, self.output_size])
                z = tf.concat(1,[p_state, p_z])
                inputs = tf.reshape(z,[-1,1,self.output_size*2])
                if c is not None:
                    o, (c, state) = tf.nn.dynamic_rnn( cell, inputs = inputs, initial_state = tf.nn.rnn_cell.LSTMStateTuple(c, state), dtype=tf.float32)
                else:
                    o, (c, state) = tf.nn.dynamic_rnn(cell, inputs=inputs, dtype=tf.float32)
                fw_states.append(state)
                tf.get_variable_scope().reuse_variables()
        fw_states = tf.pack(fw_states)
        fw_states = tf.transpose(fw_states, perm=(1,0,2))

        bk_states = []
        with tf.variable_scope("Backward_Match-LSTM"):
            cell = tf.nn.rnn_cell.LSTMCell(self.output_size, initializer=tf.contrib.layers.xavier_initializer())
            W_q = tf.get_variable("W_q", shape=(self.output_size, self.output_size), initializer=tf.contrib.layers.xavier_initializer())
            W_r = tf.get_variable("W_r", shape=(self.output_size, self.output_size), initializer=tf.contrib.layers.xavier_initializer())
            b_p = tf.get_variable("b_p", shape=(self.output_size), initializer=tf.contrib.layers.xavier_initializer())
            w = tf.get_variable("w", shape=(self.output_size,1), initializer=tf.contrib.layers.xavier_initializer())
            b = tf.get_variable("b", shape=(1,1), initializer=tf.contrib.layers.xavier_initializer())
            state = None
            c = None
            for time_step in range(paragraph_length):
                p_state = paragraph_states[:, time_step, :]
                X_ = tf.reshape(questions_states, [-1, self.output_size])
                if state is not None:
                    G = tf.nn.tanh(
                        tf.matmul(X_, W_q) + tf.matmul(p_state, W_r) + tf.matmul(state, W_r) + b_p)  # batch_size*Q,l
                else:
                    G = tf.nn.tanh(
                        tf.matmul(X_, W_q) + tf.matmul(p_state, W_r) + b_p)  # batch_size*Q,l
                atten = tf.nn.softmax(tf.matmul(G, w) + b)  # batch_size*Q,1
                atten = tf.reshape(atten, [-1, 1, question_length])
                X_ = tf.reshape(questions_states, [-1, question_length, self.output_size])
                p_z = tf.matmul(atten, X_)
                p_z = tf.reshape(p_z, [-1, self.output_size])
                z = tf.concat(1, [p_state, p_z])
                inputs = tf.reshape(z, [-1, 1, self.output_size * 2])
                if c is not None:
                    o, (c, state) = tf.nn.dynamic_rnn(cell, inputs=inputs,
                                                      initial_state=tf.nn.rnn_cell.LSTMStateTuple(c, state),
                                                      dtype=tf.float32)
                else:
                    o, (c, state) = tf.nn.dynamic_rnn(cell, inputs=inputs, dtype=tf.float32)
                bk_states.append(state)
                tf.get_variable_scope().reuse_variables()
        bk_states = tf.pack(bk_states)
        bk_states = tf.transpose(bk_states, perm=(1,0,2))
        knowledge_rep =  tf.concat(2,[fw_states,bk_states])
        return knowledge_rep #None, ...


    def decode(self, knowledge_rep, paragraph_length):
        """
        takes in a knowledge representation
        and output a probability estimation over
        all paragraph tokens on which token should be
        the start of the answer span, and which should be
        the end of the answer span.
        :param knowledge_rep: it is a representation of the paragraph and question,
                              decided by how you choose to implement the encoder
        :return:
        """
        output_size = self.output_size
        # predict start index
        with tf.variable_scope("Boundary-LSTM_start"):
            cell = tf.nn.rnn_cell.LSTMCell(self.output_size, initializer=tf.contrib.layers.xavier_initializer())
            V = tf.get_variable("V", shape=(2*output_size, output_size), initializer=tf.contrib.layers.xavier_initializer())
            b_a = tf.get_variable("b_a", shape=(1, output_size), initializer=tf.contrib.layers.xavier_initializer())
            W_a = tf.get_variable("W_a", shape=(output_size, output_size), initializer=tf.contrib.layers.xavier_initializer())
            c = tf.get_variable("c", shape=(1,1), initializer=tf.contrib.layers.xavier_initializer())
            v = tf.get_variable("v", shape=(output_size,1), initializer=tf.contrib.layers.xavier_initializer())
            state = None
            c_= None
            probab_s = None
            for time_step in range(paragraph_length):
                H_r = tf.reshape(knowledge_rep, [-1, 2*output_size])
                if state is not None:
                    F_s = tf.nn.tanh(tf.matmul(H_r, V) + tf.matmul(state, W_a) +b_a)
                else:
                    F_s = tf.nn.tanh(tf.matmul(H_r, V) + b_a)
                beta_s = tf.reshape(tf.nn.softmax(tf.matmul(F_s, v) + c), shape=[-1, paragraph_length])
                if probab_s is None:
                    probab_s = beta_s
                else:
                    probab_s = probab_s * beta_s
                #attn = tf.reshape(probab_s, [-1, paragraph_length])
                #H_r = tf.reshape(knowledge_rep, [-1, paragraph_length, 2*self.output_size])
                z = tf.matmul(beta_s, H_r)
                inputs = tf.reshape(z, [-1, 1, self.output_size * 2])
                if c_ is not None:
                    o, (c_, state) = tf.nn.dynamic_rnn(cell, inputs=inputs,
                                                      initial_state=tf.nn.rnn_cell.LSTMStateTuple(c_, state),
                                                      dtype=tf.float32)
                else:
                    o, (c_, state) = tf.nn.dynamic_rnn(cell, inputs=inputs, dtype=tf.float32)
                tf.get_variable_scope().reuse_variables()

        # predict end index; beta_e is the probability distribution over the paragraph words

        with tf.variable_scope("Boundary-LSTM_end"):
            cell = tf.nn.rnn_cell.LSTMCell(self.output_size, initializer=tf.contrib.layers.xavier_initializer())
            V = tf.get_variable("V", shape=(2*output_size, output_size), initializer=tf.contrib.layers.xavier_initializer())
            b_a = tf.get_variable("b_a", shape=(1, output_size), initializer=tf.contrib.layers.xavier_initializer())
            W_a = tf.get_variable("W_a", shape=(output_size, output_size), initializer=tf.contrib.layers.xavier_initializer())
            c = tf.get_variable("c", shape=(1,1), initializer=tf.contrib.layers.xavier_initializer())
            v = tf.get_variable("v", shape=(output_size,1), initializer=tf.contrib.layers.xavier_initializer())
            state = None
            c_ =None
            probab_e = None
            for time_step in range(paragraph_length):
                H_r = tf.reshape(knowledge_rep, [-1, 2*output_size])
                if state is not None:
                    F_e = tf.nn.tanh(tf.matmul(H_r, V) + tf.matmul(state, W_a) +b_a)
                else:
                    F_e = tf.nn.tanh(tf.matmul(H_r, V) + b_a)
                beta_e = tf.reshape(tf.nn.softmax(tf.matmul(F_e, v) + c), shape=[-1, paragraph_length])
                if probab_e is None:
                    probab_e = beta_e
                else:
                    probab_e = probab_e * beta_e
                #attn = tf.reshape(probab_e, [-1, paragraph_length])
                #H_r = tf.reshape(knowledge_rep, [-1, paragraph_length, 2*self.output_size])
                z = tf.matmul(beta_e, H_r)
                inputs = tf.reshape(z, [-1, 1, self.output_size * 2])
                if c_ is not None:
                    o, (c_, state) = tf.nn.dynamic_rnn(cell, inputs=inputs,
                                                      initial_state=tf.nn.rnn_cell.LSTMStateTuple(c_, state),
                                                      dtype=tf.float32)
                else:
                    o, (c_, state) = tf.nn.dynamic_rnn(cell, inputs=inputs, dtype=tf.float32)
                tf.get_variable_scope().reuse_variables()

        return probab_s, probab_e #[None, 766]


class QASystem(object):
    def __init__(self, encoder, decoder, args, pretrained_embeddings):
        """
        Initializes your System
        :param encoder: an encoder that you constructed in train.py
        :param decoder: a decoder that you constructed in train.py
        :param args: pass in more arguments as needed
        """
        self.encoder = encoder
        self.decoder = decoder
        self.config = args
        self.pretrained_embeddings = pretrained_embeddings
        # ==== set up placeholder tokens ========
        self.p_max_length = self.config.paragraph_size
        self.embed_size = encoder.vocab_dim
        self.q_max_length = self.config.question_size
        self.q_placeholder = tf.placeholder(tf.int32, (None, self.q_max_length))
        self.p_placeholder = tf.placeholder(tf.int32, (None, self.p_max_length))
        self.answer_span_placeholder_start = tf.placeholder(tf.int32, (None))
        self.answer_span_placeholder_end = tf.placeholder(tf.int32, (None))
        self.q_mask_placeholder = tf.placeholder(tf.bool, (None, self.q_max_length))
        self.p_mask_placeholder = tf.placeholder(tf.bool, (None, self.p_max_length))
        self.dropout_placeholder = tf.placeholder(tf.float32, (None))

        # ==== assemble pieces ====
        with tf.variable_scope("qa", initializer=tf.uniform_unit_scaling_initializer(1.0)):
            self.setup_embeddings()
            self.setup_system()
            self.preds = self.decoder.decode(self.knowledge_rep, self.p_max_length)
            self.setup_loss(self.preds)

        # ==== set up training/updating procedure ====
        optfn = get_optimizer(self.config.optimizer)
        self.train_op = optfn(self.config.learning_rate).minimize(self.loss)

    def setup_system(self):
        """
        After your modularized implementation of encoder and decoder
        you should call various functions inside encoder, decoder here
        to assemble your reading comprehension system!
        :return:
        """
        encoded_q, self.q_states = self.encoder.encode_questions(self.q_embeddings, self.q_mask_placeholder, None)
        encoded_p, self.p_states = self.encoder.encode_w_attn(self.p_embeddings, self.p_mask_placeholder, self.q_states,
                                                              scope="", reuse=False)

        self.knowledge_rep = self.decoder.match_LASTM(self.q_states, self.p_states, self.q_max_length,
                                                      self.p_max_length, self.config.dropout)

    def setup_loss(self, preds):
        """
        Set up your loss computation here
        :return:
        """
        with vs.variable_scope("loss"):
            loss_tensor = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=self.answer_span_placeholder_start, logits = preds[0])
            start_index_loss = tf.reduce_mean(loss_tensor, 0)
            loss_tensor = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=self.answer_span_placeholder_end, logits=preds[1])
            end_index_loss = tf.reduce_mean(loss_tensor, 0)
            self.loss = [start_index_loss , end_index_loss]

    def setup_embeddings(self):
        """
        Loads distributed word representations based on placeholder tokens
        :return:
        """
        with vs.variable_scope("embeddings"):
            self.pretrained_embeddings = tf.Variable(self.pretrained_embeddings, trainable=False, dtype=tf.float32)
            q_embeddings = tf.nn.embedding_lookup(self.pretrained_embeddings, self.q_placeholder)
            self.q_embeddings = tf.reshape(q_embeddings, shape=[-1, self.config.question_size, 1 * self.embed_size])
            p_embeddings = tf.nn.embedding_lookup(self.pretrained_embeddings, self.p_placeholder)
            self.p_embeddings = tf.reshape(p_embeddings, shape=[-1, self.config.paragraph_size, 1 * self.embed_size])

    def optimize(self, session, dataset, mask, dropout=1):
        """
        Takes in actual data to optimize your model
        This method is equivalent to a step() function
        :return:
        """
        input_feed = {}
        if dataset is not None:
            input_feed[self.q_placeholder] = dataset['Questions']
            input_feed[self.p_placeholder] = dataset['Paragraphs']
            input_feed[self.answer_span_placeholder_start] =  dataset['Labels'][:,0]
            input_feed[self.answer_span_placeholder_end] =  dataset['Labels'][:,1]
        if mask is not None:
            input_feed[self.q_mask_placeholder] = dataset['Questions_masks']
            input_feed[self.p_mask_placeholder] = dataset['Paragraphs_masks']
        input_feed[self.dropout_placeholder] = dropout
        # fill in this feed_dictionary like:
        # input_feed['train_x'] = train_x

        output_feed = []
        train_op_start = tf.train.AdamOptimizer(self.config.learning_rate).minimize(self.start_index_loss)
        output_feed = [train_op_start, self.start_index_loss]
        start_index_pred = session.run(output_feed, input_feed)
        train_op_end = tf.train.AdamOptimizer(self.config.learning_rate).minimize(self.end_index_loss)
        output_feed = [train_op_end, self.end_index_loss]
        end_index_pred = session.run(output_feed, input_feed)

        return start_index_pred, end_index_pred

    def test(self, session, valid_x, valid_y):
        """
        in here you should compute a cost for your validation set
        and tune your hyperparameters according to the validation set performance
        :return:
        """
        input_feed = {}

        # fill in this feed_dictionary like:
        # input_feed['valid_x'] = valid_x
        # feed = self.create_feed_dict(inputs_batch)
        # predictions = sess.run(self.pred, feed_dict=feed)
        output_feed = []

        outputs = session.run(output_feed, input_feed)

        return outputs

    def decode(self, session, train_x, mask):
        """
        Returns the probability distribution over different positions in the paragraph
        so that other methods like self.answer() will be able to work properly
        :return:
        """
        input_feed = {}
        if train_x is not None:
            input_feed[self.q_placeholder] = train_x['Questions']
            input_feed[self.p_placeholder] = train_x['Paragraphs']
        if mask is not None:
            input_feed[self.q_mask_placeholder] = train_x['Questions_masks']
            input_feed[self.p_mask_placeholder] = train_x['Paragraphs_masks']
        # fill in this feed_dictionary like:
        # input_feed['test_x'] = test_x

        output_feed = [self.preds]
        outputs = session.run(output_feed, input_feed)

        return outputs

    def create_feed_dict(self, question_batch, context_batch, labels_batch=None):
        """Creates the feed_dict for the model.
        NOTE: You do not have to do anything here.
        """
        feed_dict = {}
        print(len(question_batch))
        print(len(context_batch))
        print(len(labels_batch))
        feed_dict[self.q_placeholder] = question_batch
        print("questionBatch" + str(len(question_batch)) + 'by' + str(len(question_batch[0])) + str(
            tf.shape(self.q_placeholder)))
        feed_dict[self.p_placeholder] = context_batch
        print("contextBatch" + str(len(context_batch)) + 'by' + str(len(context_batch[0])) + str(
            tf.shape(self.p_placeholder)))
        if labels_batch is not None:

            feed_dict[self.answer_span_placeholder_start] = labels_batch[:,0]
            feed_dict[self.answer_span_placeholder_end] = labels_batch[:, 1]
            #print("Labels" + str(len(labels_batch[0])) + str(tf.shape(self.answer_span_placeholder_start))
        return feed_dict

    def train_on_batch(self, session, question_batch, context_batch, label_batch):
        feed_dict = self.create_feed_dict(question_batch, context_batch, label_batch);
        _, loss = session.run([self.train_op, self.loss], feed_dict=feed_dict)
        return loss

    def run_epoch(self, sess, inputs):
        """Runs an epoch of training.
        Args:
            sess: tf.Session() object
            inputs: datasets represented as a dictionary
            labels: np.ndarray of shape (n_samples, n_classes)
        Returns:
            average_loss: scalar. Average minibatch loss of model on epoch.
        """
        n_minibatches, total_loss = 0, 0
        for batch in get_minibatches([inputs['Questions'], inputs['Paragraphs'], inputs['Labels']],
                                     self.config.batch_size):
            n_minibatches += 1
            total_loss += self.train_on_batch(sess, *batch)
        return total_loss / n_minibatches

    def answer(self, session, test_x, mask):

        yp, yp2 = self.decode(session, test_x, mask)
        a_s = np.argmax(yp, axis=1)
        a_e = np.argmax(yp2, axis=1)
        return (a_s, a_e)

    def validate(self, sess, valid_dataset):
        """
        Iterate through the validation dataset and determine what
        the validation cost is.
        This method calls self.test() which explicitly calculates validation cost.
        How you implement this function is dependent on how you design
        your data iteration function
        :return:
        """
        valid_cost = 0

        for valid_x, valid_y in valid_dataset:
            valid_cost = self.test(sess, valid_x, valid_y)

        return valid_cost

    def evaluate_answer(self, session, dataset, sample=100, log=False):
        """
        Evaluate the model's performance using the harmonic mean of F1 and Exact Match (EM)
        with the set of true answer labels
        This step actually takes quite some time. So we can only sample 100 examples
        from either training or testing set.
        :param session: session should always be centrally managed in train.py
        :param dataset: a representation of our data, in some implementations, you can
                        pass in multiple components (arguments) of one dataset to this function
        :param sample: how many examples in dataset we look at
        :param log: whether we print to std out stream
        :return:
        """
        idx_sample = np.random.randint(0, dataset['Questions'].shape[0], sample)
        examples = {}
        examples['Questions'] = dataset['Questions'][idx_sample]
        examples['Paragraphs'] = dataset['Paragraphs'][idx_sample]
        examples['Questions_masks'] = dataset['Questions'][idx_sample]
        examples['Paragraphs_masks'] = dataset['Paragraphs'][idx_sample]
        examples['Labels'] = dataset['Labels'][idx_sample]

        correct_preds, total_correct, total_preds = 0., 0., 0.
        masks = True
        for _, labels, labels_ in self.answer(session, examples, masks):
            pred = set()
            if labels_[0] <= labels_[1]:
                pred = set(range(labels_[0], labels_[1] + 1))
            gold = set(range(labels[0], labels[1] + 1))

            correct_preds += len(gold.intersection(pred))
            total_preds += len(pred)
            total_correct += len(gold)

        p = correct_preds / total_preds if correct_preds > 0 else 0
        r = correct_preds / total_correct if correct_preds > 0 else 0
        f1 = 2 * p * r / (p + r) if correct_preds > 0 else 0
        em = correct_preds

        if log:
            logging.info("F1: {}, EM: {}, for {} samples".format(f1, em, sample))

        return f1, em

    def train(self, session, dataset, train_dir):
        """
        Implement main training loop
        TIPS:
        You should also implement learning rate annealing (look into tf.train.exponential_decay)
        Considering the long time to train, you should save your model per epoch.
        More ambitious appoarch can include implement early stopping, or reload
        previous models if they have higher performance than the current one
        As suggested in the document, you should evaluate your training progress by
        printing out information every fixed number of iterations.
        We recommend you evaluate your model performance on F1 and EM instead of just
        looking at the cost.
        :param session: it should be passed in from train.py
        :param dataset: a representation of our data, in some implementations, you can
                        pass in multiple components (arguments) of one dataset to this function
        :param train_dir: path to the directory where you should save the model checkpoint
        :return:
        """

        # some free code to print out number of parameters in your model
        # it's always good to check!
        # you will also want to save your model parameters in train_dir
        # so that you can use your trained model to make predictions, or
        # even continue training

        results_path = os.path.join(train_dir, "{:%Y%m%d_%H%M%S}".format(datetime.now()))
        tic = time.time()
        params = tf.trainable_variables()
        num_params = sum(map(lambda t: np.prod(tf.shape(t.value()).eval()), params))
        toc = time.time()
        logging.info("Number of params: %d (retreival took %f secs)" % (num_params, toc - tic))
        best_score = 0.
        for epoch in range(self.config.epochs):
            print("Epoch %d out of %d", epoch + 1, self.config.epochs)
            print("Best score so far: " + str(best_score))
            loss = self.run_epoch(session, dataset)
            f1, em = self.evaluate_answer(session, dataset, sample=800, log=True)
            print("loss: " + str(loss) + " f1: " + str(f1) + " em:" + str(em))
            if f1 > best_score:
                best_score = f1
                logging.info("New best score! Saving model in %s", results_path)
                if self.saver:
                    self.saver.save(session, results_path)
            print("")

        return best_score

