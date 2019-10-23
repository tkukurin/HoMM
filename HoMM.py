from __future__ import print_function
from __future__ import absolute_import
from __future__ import division

import numpy as np
import tensorflow as tf
import tensorflow.contrib.slim as slim
import os
import re

from configs.default_architecture_config import default_architecture_config


### END PARAMATERS (finally) ##################################
class memory_buffer(object):
    """Essentially a wrapper around numpy arrays that handles inserting and
    removing."""
    def __init__(self, length, input_width, outcome_width):
        self.length = length 
        self.curr_index = 0 
        self.input_buffer = np.zeros([length, input_width])
        self.outcome_buffer = np.zeros([length, outcome_width])

    def insert(self, input_mat, outcome_mat):
        num_events = len(input_mat)
        if num_events > self.length:
            num_events = length
            self.input_buffer = input_mat[-length:, :]
            self.outcome_buffer = outcome_mat[-length:, :]
            self.curr_index = 0.
            return
        end_offset = num_events + self.curr_index
        if end_offset > self.length: 
            back_off = self.length - end_offset
            num_to_end = num_events + back_off
            self.input_buffer[:-back_off, :] = input_mat[num_to_end:, :] 
            self.outcome_buffer[:-back_off, :] = outcome_mat[num_to_end:, :] 
        else: 
            back_off = end_offset
            num_to_end = num_events
        self.input_buffer[self.curr_index:back_off, :] = input_mat[:num_to_end, :] 
        self.outcome_buffer[self.curr_index:back_off, :] = outcome_mat[:num_to_end, :] 
        self.curr_index = np.abs(back_off)

    def get_memories(self): 
        return self.input_buffer, self.outcome_buffer


# default IO nets are those used for the original polynomials experiments
def default_input_processor(input_ph, IO_num_hidden, z_dim, internal_nonlinearity):
        input_processing_1 = slim.fully_connected(input_ph, IO_num_hidden,
                                                  activation_fn=internal_nonlinearity)

        input_processing_2 = slim.fully_connected(input_processing_1, IO_num_hidden,
                                                  activation_fn=internal_nonlinearity)

        processed_input = slim.fully_connected(input_processing_2, z_dim,
                                               activation_fn=None)
        return processed_input


def default_target_processor(targets, IO_num_hidden, z_dim, internal_nonlinearity):
    output_size = sum(tf.get_shape(targets)[1:])
    target_processor_nontf = random_orthogonal(z_dim)[:, :output_size + 1]
    target_processor = tf.get_variable('target_processor',
                                        shape=[num_hidden_hyper, output_size],
                                        initializer=tf.constant_initializer(target_processor_nontf[:, :-1))
    meta_class_processor = tf.get_variable('meta_class_processor',
                                        shape=[num_hidden_hyper, output_size],
                                        initializer=tf.constant_initializer(meta_class_processor_nontf[:, -1:))
    
    processed_targets = tf.matmul(targets, tf.transpose(target_processor))
    return processed_targets, target_processor, meta_class_processor


def default_meta_class_processor(targets, meta_class_processor):
    processed_targets = tf.matmul(targets, tf.transpose(meta_class_processor))
    return processed_targets


def default_output_processor(output_embeddings, target_processor):
    processed_outputs = tf.matmul(output_embeddings, target_processor)
    return processed_outputs 


def default_base_loss(outputs, targets):
    return tf.nn.l2_loss(outputs - targets)


def default_meta_loss(outputs, targets):
    batch_losses = tf.reduce_sum(tf.square(outputs - targets), axis=1)
    return tf.reduce_mean(batch_losses)


def _pad(l, length, pad_token="PAD"):
    return [pad_token]*(length - len(l)) + l


def save_config(filename, config):
    with open(filename, "w") as fout:
        fout.write("key, value\n")
        for key, value in config.items():
            fout.write(key + ", " + str(value) + "\n")


class HoMM_model(object):
    """A base Homoiconic Meta-mapping model."""
    def __init__(self, run_config, architecture_config=None,
                 input_processor=None, output_processor=None,
                 base_loss=None, meta_loss=None):
        """Set up data structures, build the network.
        
        The child classes should call this with a super call, overriding the
        _pre_ and _post_build_calls methods as necessary. In particular, the
        _pre_build_calls function should be used to add any
        """
        self.run_config = run_config

        self.num_tasks = 0
        self.base_train = [] 
        self.base_eval = [] 
        self.meta_class_train = []
        self.meta_class_eval = []
        self.meta_map_train = []
        self.meta_map_eval = []

        self.meta_pairings = {}

        self._pre_build_calls()  # tasks should be added to lists here

        self._task_processing()

        self._build_architecture(
            self, architecture_config=None, input_processor=None
            output_processor=None, base_loss=None, meta_loss=None)
        self._post_build_calls()
        self._sess_and_init()


    def _pre_build_calls(self):
        """Should be overridden to do something before building the net."""
        raise NotImplementedError("You must override the _pre_build_calls "
                                  "method to add your base and meta tasks.")


    def _task_processing(self):
        """Create the memory buffers, etc. for the tasks."""
        
        # update config
        self.run_config["base_train"] = self.base_train
        self.run_config["base_eval"] = self.base_eval
        self.run_config["meta_class_train"] = self.meta_class_train
        self.run_config["meta_class_eval"] = self.meta_class_eval
        self.run_config["meta_map_train"] = self.meta_map_train
        self.run_config["meta_map_eval"] = self.meta_map_eval

        self.task_indices = {}
        # create buffers for base tasks
        self.memory_buffers = {}
        for task in self.base_train + self.base_eval:
            self.base_task_lookup(task)


        # create structures for meta tasks
        self.meta_datasets = {}
        all_meta_tasks = self.meta_class_train + self.meta_class_eval + self.meta_map_train + self.meta_map_eval
        for mt in all_meta_tasks:
            self.meta_task_lookup(mt)

        # populate meta-task structures
        for mts, meta_class in zip([self.meta_class_train + self.meta_class_eval,
                                      self.meta_map_train + self.meta_map_eval],
                                     [True, False]):
            out_dtype = np.float32 if meta_class else np.int32
            for mt in mts:
                self.meta_dataset[mt] = {} 

                train_pairings = self.meta_pairings[mt]["train"]
                num_train = len(train_pairings)
                eval_pairings = self.meta_pairings[mt]["eval"]
                num_eval = len(eval_pairings)

                self.meta_datasets[mt]["train"] = {}
                self.meta_datasets[mt]["eval"] = {}
                self.meta_datasets[mt]["train"]["in"] = np.zeros(
                    [num_train, ], dtype=np.int32)
                self.meta_datasets[mt]["train"]["out"] = np.zeros(
                    [num_train, ], dtype=out_dtype)

                self.meta_datasets[mt]["eval"]["in"] = np.zeros(
                    [num_train + num_eval], dtype=np.int32)
                self.meta_datasets[mt]["eval"]["out"] = np.zeros(
                    [num_train + num_eval], dtype=out_dtype)
                eval_guess_mask = np.concatenate([np.ones([num_train],
                                                          dtype=np.bool),
                                                  np.zeros([num_eval],
                                                           dtype=np.bool)])

                self.meta_datasets[mt]["eval"]["gm"] = eval_guess_mask

                for i, (e, res) in enumerate(train_pairings):
                    e_index = self.environment_indices[e]
                    self.meta_datasets[mt]["train"]["in"][i] = e_index
                    if meta_class:
                        self.meta_datasets[mt]["train"]["out"][i] = res
                    else:
                        res_index = self.environment_indices[res]
                        self.meta_datasets[mt]["train"]["out"][i] = res_index

                self.meta_datasets[mt]["eval"]["in"][:num_train] = self.meta_datasets[mt]["train"]["in"]
                self.meta_datasets[mt]["eval"]["out"][:num_train] = self.meta_datasets[mt]["train"]["out"]

                for i, (e, res) in enumerate(eval_pairings):
                    e_index = self.environment_indices[e]
                    self.meta_datasets[mt]["eval"]["in"][num_train + i] = e_index
                    if meta_class:
                        self.meta_datasets[mt]["eval"]["out"][num_train + i] = res
                    else:
                        res_index = self.environment_indices[res]
                        self.meta_datasets[mt]["eval"]["out"][num_train + i] = res_index

    def _post_build_calls(self):
        """Can be overridden to do something after building, before session."""
        pass

    def _build_architecture(self, architecture_config=None, input_processor=None
                            output_processor=None, base_loss=None, meta_loss=None):
        if architecture_config is None:
            config = default_architecture_config 
        else:
            config = architecture_config
        self.config = config
        self.memory_buffer_size = config["memory_buffer_size"]
        self.meta_batch_size = config["meta_batch_size"]
        self.num_input = config["num_input"]
        self.num_output = config["num_output"]
        self.tkp = 1. - config["train_drop_prob"] # drop prob -> keep prob
        self.vocab = config["vocab"]
        self.vocab_size = len(self.vocab)

        # network
        var_scale_init = tf.contrib.layers.variance_scaling_initializer(factor=1., mode='FAN_AVG')

        # base task input (polynomials, need to refactor)
        input_shape = config["input_shape"]
        output_shape = config["output_shape"]

        self.base_input_ph = tf.placeholder(
            tf.float32, shape=[None] + input_shape)
        self.base_target_ph = tf.placeholder(
            tf.float32, shape=[None] + output_shape)

        self.lr_ph = tf.placeholder(tf.float32)
        self.keep_prob_ph = tf.placeholder(tf.float32) # dropout keep prob

        F_num_hidden = config["F_num_hidden"]
        IO_num_hidden = config["IO_num_hidden"]
        num_hidden_hyper = config["H_num_hidden"]
        z_dim = config["z_dim"]
        internal_nonlinearity = config["internal_nonlinearity"]
        output_nonlinearity = config["output_nonlinearity"]

        if input_processor is None:
            input_processor = lambda x: default_input_processor(
                x, IO_num_hidden, z_dim, internal_nonlinearity)
        self.processed_input = input_processor(self.base_input_ph)

        if target_processor is None:
            target_processor = lambda x: default_target_processor(
                x, IO_num_hidden, z_dim, internal_nonlinearity)
            (processed_targets, target_processor_var,
             meta_class_processor_var) = target_processor(self.base_target_ph)
        else:
            if output_processor is None or meta_class_processor is None:
                raise ValueError("You cannot use the default output or "
                                 "meta_class processor without the default "
                                 "target processor.")
            processed_targets = target_processor(self.base_target_ph)

        if output_processor is None:
            output_processor = lambda x: default_output_processor(
                x, target_processor_var)

        ## Meta: (Input, Output) -> Z (function embedding)
        self.task_index_ph = tf.placeholder(tf.int32, [None,])  # if using persistent task reps

        self.meta_input_indices_ph = tf.placeholder(tf.int32, shape=[None, ])
        self.meta_target_indices_ph = tf.placeholder(tf.int32, [None,])

        self.meta_class_ph = tf.placeholder(tf.float32, shape=[None, 1]) # for meta classification tasks

        if meta_class_processor is None:
            meta_class_processor = lambda x: default_meta_class_processor(
                x, meta_class_processor_var)
        
        processed_class = meta_class_processor(self.meta_class_ph) 

        # function embedding "guessing" network / meta network
        # {(emb_in, emb_out), ...} -> emb
        self.guess_input_mask_ph = tf.placeholder(tf.bool, shape=[None]) # which datapoints get excluded from the guess

        def _meta_network(embedded_inputs, embedded_targets,
                          mask_ph=self.guess_input_mask_ph, reuse=True):
            num_hidden_meta = config["M_num_hidden"]
            with tf.variable_scope('meta', reuse=reuse):
                meta_input = tf.concat([embedded_inputs,
                                        embedded_targets], axis=-1)
                meta_input = tf.boolean_mask(meta_input,
                                             mask)

                mh_1 = slim.fully_connected(meta_input, num_hidden_meta,
                                            activation_fn=internal_nonlinearity)
                mh_2 = slim.fully_connected(mh_1, num_hidden_meta,
                                            activation_fn=internal_nonlinearity)
                if config["meta_max_pool"]:
                    mh_2b = tf.reduce_max(mh_2, axis=0, keep_dims=True)
                else:
                    mh_2b = tf.reduce_mean(mh_2, axis=0, keep_dims=True)

                mh_3 = slim.fully_connected(mh_2b, num_hidden_meta,
                                            activation_fn=internal_nonlinearity)

                guess_embedding = slim.fully_connected(mh_3, config["z_dim"],
                                                       activation_fn=None)
                return guess_embedding


        self.base_guess_emb = _meta_network(processed_input,
                                            processed_targets,
                                            reuse=False)

        # TODO: Use it or lose it
        # for combination tasks 
#        def _combine_inputs(inputs_1, inputs_2, reuse=True):
#            with tf.variable_scope('meta/bi_combination', reuse=reuse):
#                c_input = tf.concat([inputs_1,
#                                     inputs_2], axis=-1)
#                c_input = tf.nn.dropout(c_input, self.keep_prob_ph)
#
#                ch_1 = slim.fully_connected(c_input, num_hidden_hyper,
#                                            activation_fn=internal_nonlinearity)
#                ch_1 = tf.nn.dropout(ch_1, self.keep_prob_ph)
#                ch_2 = slim.fully_connected(ch_1, num_hidden_hyper,
#                                            activation_fn=internal_nonlinearity)
#                ch_2 = tf.nn.dropout(ch_2, self.keep_prob_ph)
#                ch_3 = slim.fully_connected(ch_2, num_hidden_hyper,
#                                            activation_fn=internal_nonlinearity)
#                combined = tf.nn.dropout(ch_3, self.keep_prob_ph)
#                # also include direct inputs averaged
#                self.combination_weight = tf.get_variable('combination_weight',
#                                                          shape=[],
#                                                          initializer=tf.constant_initializer(0.))
#                c_w = tf.nn.sigmoid(self.combination_weight) 
#                combined = (c_w) * combined + (1.- c_w) * 0.5 * (inputs_1 + inputs_2) 
#                return combined 
#
#        self.combined_meta_inputs = _combine_inputs(self.meta_input_ph,
#                                                    self.meta_input_2_ph,
#                                                    reuse=False)
#
#        self.guess_meta_bf_function_emb = _meta_network(
#            self.combined_meta_inputs, self.meta_target_ph)


        ## language processing: lang -> Z
        num_hidden_language = config["num_hidden_language"]
        num_lstm_layers = config["num_lstm_layers"]

        self.lang_keep_ph = lang_keep_ph = tf.placeholder(tf.float32)
        self.lang_keep_prob = 1. - config["lang_drop_prob"]
        self.language_input_ph = tf.placeholder(
            tf.int32, shape=[1, self.max_sentence_len])
        with tf.variable_scope("word_embeddings", reuse=False):
            self.word_embeddings = tf.get_variable(
                "embeddings", shape=[self.vocab_size, num_hidden_language])
        self.embedded_language = tf.nn.embedding_lookup(self.word_embeddings,
                                                        self.language_input_ph)

        def _language_network(embedded_language, reuse=True):
            """Maps from language to a function embedding"""
            with tf.variable_scope("language_processing"):
                cells = [tf.nn.rnn_cell.LSTMCell(
                    num_hidden_language) for _ in range(num_lstm_layers)]
                stacked_cell = tf.nn.rnn_cell.MultiRNNCell(cells)

                state = stacked_cell.zero_state(1, dtype=tf.float32)

                for i in range(self.max_sentence_len):
                    this_input = embedded_language[:, i, :]
                    this_input = tf.nn.dropout(this_input,
                                               lang_keep_ph)
                    cell_output, state = stacked_cell(this_input, state)

                cell_output = tf.nn.dropout(cell_output,
                                           lang_keep_ph)
                language_hidden = slim.fully_connected(
                    cell_output, num_hidden_language,
                    activation_fn=internal_nonlinearity)

                language_hidden = tf.nn.dropout(language_hidden,
                                           lang_keep_ph)

                func_embeddings = slim.fully_connected(language_hidden,
                                                       num_hidden_hyper,
                                                       activation_fn=None)
            return func_embeddings

        self.language_function_emb = _language_network(self.embedded_language,
                                                       False)

        ## persistent/cached embeddings
        # if not config["persistent_task_reps"], these will just be updated
        # manually. Either way, they are used for eval + meta tasks.
        with tf.variable_scope("persistent_cached"):
            self.persistent_embeddings = tf.get_variable(
                "cached_task_embeddings",
                [self.total_num_tasks,
                 config["z_dim"]],
                dtype=tf.float32)

            self.update_persistent_embeddings_ph = tf.placeholder(
                tf.float32,
                [None, config["z_dim"]])

            self.update_embeddings = tf.scatter_nd_update(
                self.persistent_embeddings,
                self.task_index_ph,
                self.update_persistent_embeddings_ph)

        def _get_persistent_embeddings(task_indices):
            persistent_embs = self.persistent_embeddings

            return tf.nn.embedding_lookup(persistent_embs,
                                          task_indices)

        if config["persistent_task_reps"]:
            def _get_combined_embedding_and_match_loss(guess_embedding, task_index,
                                                       guess_weight,
                                                       target_net=False):
                cached_embedding = _get_persistent_embeddings(task_index,
                                                              target_net=target_net)
                if guess_weight == "varied":
                    guess_weight = tf.random_uniform([], dtype=tf.float32)

                combined_embedding = guess_weight * guess_embedding + (1. - guess_weight) * cached_embedding
                emb_match_loss = config["emb_match_loss_weight"] * tf.nn.l2_loss(
                    guess_embedding - cached_embedding)
                return combined_embedding, emb_match_loss

            self.lookup_cached_emb = _get_persistent_embeddings(
                self.task_index_ph)

            (self.base_combined_emb,
             self.base_emb_match_loss) = _get_combined_embedding_and_match_loss(
                self.base_guess_emb, self.task_index_ph,
                config["combined_emb_guess_weight"])

        meta_input_embeddings = _get_persistent_embeddings(
            self.meta_input_indices_ph)
        meta_target_embeddings = _get_persistent_embeddings(
            self.meta_target_indices_ph)

        self.meta_class_guess_emb = _meta_network(meta_input_embeddings,
                                                  processed_class)
        self.meta_map_guess_emb = _meta_network(meta_input_embeddings,
                                                meta_target_embeddings)


        if config["persistent_task_reps"]:
            (self.meta_class_combined_emb,
             self.meta_class_emb_match_loss) = _get_combined_embedding_and_match_loss(
                self.meta_class_guess_emb, self.task_index_ph,
                config["combined_emb_guess_weight"])

            (self.meta_map_combined_emb,
             self.meta_map_emb_match_loss) = _get_combined_embedding_and_match_loss(
                self.meta_map_guess_emb, self.task_index_ph,
                config["combined_emb_guess_weight"])
        else:
            self.base_combined_emb = self.base_guess_emb
            self.meta_class_combined_emb = self.meta_class_guess_emb
            self.meta_map_combined_emb = self.meta_map_guess_emb


        ## hyper_network: Z -> (F: Z -> Z)
        self.feed_embedding_ph = tf.placeholder(np.float32,
                                                [1, num_hidden_hyper])

        z_dim = config["z_dim"]
        num_hidden_hyper = config["H_num_hidden"]
        num_hidden_F = config["F_num_hidden"]
        num_task_hidden_layers = config["F_num_hidden_layers"]

        tw_range = config["task_weight_weight_mult"]/np.sqrt(
            num_hidden * num_hidden_hyper) # yields a very very roughly O(1) map
        task_weight_gen_init = tf.random_uniform_initializer(-tw_range,
                                                             tw_range)

        def _hyper_network(function_embedding, reuse=True):
            with tf.variable_scope('hyper', reuse=reuse):
                hyper_hidden = function_embedding
                for _ in range(config["H_num_hidden_layers"]):
                    hyper_hidden = slim.fully_connected(hyper_hidden, num_hidden_hyper,
                                                        activation_fn=internal_nonlinearity)

                hidden_weights = []
                hidden_biases = []

                task_weights = slim.fully_connected(hyper_hidden, num_hidden_F*(z_dim +(num_task_hidden_layers-1)*num_hidden_F + z_dim),
                                                    activation_fn=None,
                                                    weights_initializer=task_weight_gen_init)

                task_weights = tf.reshape(task_weights, [-1, num_hidden_F, (z_dim + (num_task_hidden_layers-1)*num_hidden_F + z_dim)])
                task_biases = slim.fully_connected(hyper_hidden, num_task_hidden_layers * num_hidden_F + z_dim,
                                                   activation_fn=None)

                Wi = tf.transpose(task_weights[:, :, :z_dim], perm=[0, 2, 1])
                bi = task_biases[:, :num_hidden_F]
                hidden_weights.append(Wi)
                hidden_biases.append(bi)
                for i in range(1, num_task_hidden_layers):
                    Wi = tf.transpose(task_weights[:, :, z_dim+(i-1)*num_hidden_F:z_dim+i*num_hidden_F], perm=[0, 2, 1])
                    bi = task_biases[:, num_hidden_F*i:num_hidden_F*(i+1)]
                    hidden_weights.append(Wi)
                    hidden_biases.append(bi)
                Wfinal = task_weights[:, :, -z_dim:]
                bfinal = task_biases[:, -z_dim:]

                for i in range(num_task_hidden_layers):
                    hidden_weights[i] = tf.squeeze(hidden_weights[i], axis=0)
                    hidden_biases[i] = tf.squeeze(hidden_biases[i], axis=0)

                Wfinal = tf.squeeze(Wfinal, axis=0)
                bfinal = tf.squeeze(bfinal, axis=0)
                hidden_weights.append(Wfinal)
                hidden_biases.append(bfinal)
                return hidden_weights, hidden_biases

        self.base_task_params = _hyper_network(self.base_combined_emb,
                                               reuse=False)

        self.meta_map_task_params = _hyper_network(self.guess_meta_t_function_emb)
        self.meta_map_task_params = _hyper_network(self.meta_map_combined_emb)
        self.meta_class_task_params = _hyper_network(self.meta_class_combined_emb)

        self.lang_task_params = _hyper_network(self.language_function_emb)
        self.fed_emb_task_params = _hyper_network(self.feed_embedding_ph)

        self.cached_emb_task_params = _hyper_network(self.lookup_cached_emb)

        ## task network F: Z -> Z
        def _task_network(task_params, processed_input):
            hweights, hbiases = task_params
            task_hidden = processed_input
            for i in range(num_task_hidden_layers):
                task_hidden = internal_nonlinearity(
                    tf.matmul(task_hidden, hweights[i]) + hbiases[i])

            raw_output = tf.matmul(task_hidden, hweights[-1]) + hbiases[-1]

            return raw_output

        self.base_raw_output = _task_network(self.base_task_params,
                                             processed_input)
        self.base_output = output_processor(self.base_raw_output)

        self.base_lang_raw_output = _task_network(self.lang_task_params,
                                                 processed_input)
        self.base_lang_output = output_processor(self.base_lang_raw_output)

        self.base_raw_output_fed_emb = _task_network(self.fed_emb_task_params,
                                                     processed_input)
        self.base_output_fed_emb = output_processor(
            self.base_raw_output_fed_emb)

        self.meta_class_raw_output = _task_network(self.meta_class_task_params,
                                                   meta_input_embeddings)
        self.meta_class_output = tf.nn.sigmoid(self.meta_class_raw_output)

        self.meta_map_output = _task_network(self.meta_map_task_params,
                                             meta_input_embeddings) 
        self.meta_map_lang_output = _task_network(self.lang_task_params,
                                                  meta_input_embeddings) 

        self.base_cached_emb_raw_output = _task_network(
            self.cached_emb_task_params, processed_input)
        self.base_cached_emb_output = output_processor(
            self.base_cached_emb_raw_output)

        self.meta_cached_emb_raw_output = _task_network(
            self.cached_emb_task_params, meta_input_embeddings)

        ## Losses 

        if base_loss is None:
            base_loss = default_base_loss
        if meta_loss is None:
            meta_loss = default_meta_loss

        self.total_base_loss = base_loss(self.base_output, self.base_target_ph) 

        self.total_base_lang_loss = base_loss(self.base_lang_output,
                                              self.base_target_ph) 

        self.total_base_fed_emb_loss = base_loss(self.base_output_fed_emb,
                                                 self.base_target_ph) 

        self.total_meta_class_loss = meta_loss(self.meta_class_output,
                                               processed_class) 

        self.total_meta_map_loss = meta_loss(self.meta_map_output,
                                             meta_target_embeddings) 


        self.total_base_cached_emb_loss = base_loss(
            self.base_cached_emb_output, self.base_target_ph) 

        self.total_meta_class_cached_emb_loss = meta_loss(
            self.meta_cached_emb_raw_output, processed_class) 

        self.total_meta_map_cached_emb_loss = meta_loss(
            self.meta_map_output, meta_target_embeddings) 

        if persistent_task_reps: # Add the emb_match losses
            self.total_base_loss += self.base_emb_match_loss
            self.total_meta_class_loss += self.meta_class_emb_match_loss
            self.total_meta_map_loss += self.meta_map_emb_match_loss

        if config["optimizer"] == "Adam":
            optimizer = tf.train.AdamOptimizer(self.lr_ph)
        elif config["optimizer"] == "RMSProp":
            optimizer = tf.train.RMSPropOptimizer(self.lr_ph)
        else:
            raise ValueError("Unknown optimizer: %s" % config["optimizer"])

        self.base_train = optimizer.minimize(self.total_base_loss)
        self.base_lang_train = optimizer.minimize(self.total_base_lang_loss)
        self.meta_class_train = optimizer.minimize(self.total_meta_class_loss)
        self.meta_map_train = optimizer.minimize(self.total_meta_map_loss)


    def _sess_and_init(self):
        # Saver
        self.saver = tf.train.Saver()

        # initialize
        sess_config = tf.ConfigProto()
        sess_config.gpu_options.allow_growth = True
        self.sess = tf.Session(config=sess_config)
        self.sess.run(tf.global_variables_initializer())
        self.fill_buffers(num_data_points=self.run_config["memory_buffer_size"])

        self.refresh_meta_datasets()
       
        self.save_config(# TODO, save both configs


    def get_new_memory_buffer(self):
        """Can be overriden by child"""
        return memory_buffer(self.run_config["memory_buffer_size"],
                             self.run_config["input_shape"][0],
                             self.run_config["output_shape"][0])

    def base_task_lookup(self, task):
        if isinstance(task, str):
            task_name = task
        else:
            task_name = str(task)

        if task_name not in self.task_indices:
            self.memory_buffers[task_name] = self.get_new_memory_buffer() 
            self.task_indices[task_name] = self.num_tasks
            self.num_tasks += 1

        return (task_name,
                self.memory_buffers[task_name],
                self.task_indices[task_name])

    def meta_task_lookup(self, task):
        if task not in self.task_indices:
            self.meta_datasets[task_name] = {} 
            self.task_indices[task] = self.num_tasks
            self.num_tasks += 1

        return (task_name,
                self.meta_datasets[task_name],
                self.task_indices[task_name])

    def fill_buffers(self, num_data_points=1, include_new=False):
        """Add new "experiences" to memory buffers."""
        raise NotImplementedError("fill_buffers() should be overridden by the child class!")


    def _random_guess_mask(self, dataset_length, meta_batch_size=None):
        if meta_batch_size is None:
            meta_batch_size = config["meta_batch_size"]
        mask = np.zeros(dataset_length, dtype=np.bool)
        indices = np.random.permutation(dataset_length)[:meta_batch_size]
        mask[indices] = True
        return mask


    def sample_from_memory_buffer(self, memory_buffer):
        """Return experiences from the memory buffer.
        
        The results are expected to be a set of inputs and targets, so this
        will likely need to be overridden for other settings such as RL. 
        """
    input_buff, output_buff = memory_buffer.get_memories()
    return input_buff, output_buff


    def intify_task(self, task): 
        """Will need to be overridden"""
        raise NotImplementedError("intify task should be overridden by the child class!")

    def build_feed_dict(self, task, lr=None, fed_embedding=None,
                        call_type="base_standard_train"):
        """Build a feed dict.

        This function should be overridden by the child class if the necessary.
        
        Args:
            task: the task def.
            lr: the learning rate if training.
            fed_embedding: embedding to use for performing the task, if any.
            call_type: one of "base_standard_train", "base_lang_train",
                "base_lang_eval", "base_fed_eval", "base_cached_eval",
                "metaclass_standard_train", "metamap_standard_train", 
                "metaclass_cached_eval", "metamap_cached_eval", 
                "metaclass_lang_train", "metamap_lang_train", 
                "metaclass_lang_eval", "metamap_lang_eval"
        """
        feed_dict = {}

        base_or_meta, call_type, train_or_eval = call_type.split("_")

        if base_or_meta == "base":
            task_name, memory_buffer, task_index = self.base_task_lookup(task)
            inputs, outputs = self.sample_from_memory_buffer(memory_buffer0
            feed_dict[self.base_input_ph] = inputs
            feed_dict[self.base_target_ph] = outputs
            feed_dict[self.guess_input_mask_ph] = self._random_guess_mask(
                len(outputs))
        else: # meta call
            task_name, meta_dataset, task_index = self.meta_task_lookup(task)
            meta_input_indices = meta_dataset[train_or_eval]["in"],
            feed_dict[self.meta_input_indices_ph] = meta_input_indices
            if train_or_eval == "train":
                guess_mask = self._random_guess_mask(
                    len(meta_input_indices))
            else:
                guess_mask = meta_dataset["eval"]["gm"]  # eval on the right tasks
            feed_dict[self.guess_input_mask_ph] = guess_mask 
            meta_targets = meta_dataset[train_or_eval]["out"]
            if base_or_meta == "metamap":
                feed_dict[self.meta_target_indices_ph] = meta_targets
            else:
                feed_dict[self.meta_class_ph] = meta_targets

        if call_type == "fed":
            feed_dict[self.feed_embedding_ph] = fed_embedding
        elif call_type == "lang":
            feed_dict[self.language_input_ph] = self.intify_task(task_name)

        if train_or_eval == "train":
            feed_dict[self.lr_ph] = lr
            feed_dict[self.keep_prob_ph] = self.tkp
            if call_type == "lang":
                feed_dict[self.lang_keep_prob_ph] = self.lang_keep_prob
        else:
            feed_dict[self.keep_prob_ph] = 1.
            if call_type == "lang":
                feed_dict[self.lang_keep_prob_ph] = 1. 

        return feed_dict


    def base_train_step(self, task, lr):
        feed_dict = self.build_feed_dict(task, lr=lr, call_type="base_standard_train")
        self.sess.run(self.base_train, feed_dict=feed_dict)


    def base_language_train_step(self, intified_task, lr):
        feed_dict = self.build_feed_dict(task, lr=lr, call_type="base_lang_train")
        self.sess.run(self.base_lang_train, feed_dict=feed_dict)


    def base_eval(self, task):
        feed_dict = self.build_feed_dict(task, call_type="base_cached_eval")
        fetches = [self.total_base_cached_emb_loss]
        res = self.sess.run(fetches, feed_dict=feed_dict)
        return res


    def run_base_eval(self):
        """Run evaluation on basic tasks."""
        assert(False)  # actually the below should be moved to a run_all_eval function for efficiency
        if not self.run_config["persistent_task_reps"]:
            self.refresh_base_embeddings() # make sure we're up to date

        base_tasks = self.base_train + self.base_eval 

        losses = [] 
        for task in base_tasks:
            res = self.base_eval(task)
            losses.append(res[0])

        names = [_stringify_polynomial(t) for t in base_tasks]
        return names, losses
        

    def base_language_eval(self, task):
        feed_dict = self.build_feed_dict(task, call_type="base_lang_eval")
        fetches = [self.total_base_lang_loss]
        res = self.sess.run(fetches, feed_dict=feed_dict)
        return res

    def run_base_language_eval(self):
        base_tasks = self.base_train + self.base_eval 

        losses = [] 
        names = []
        for task in tasks:
            res = self.base_language_eval(task)
            losses.append(res[0])
            names.append(task_str)

        return names, losses

    def base_embedding_eval(self, embedding, task):
        feed_dict = self.build_feed_dict(task, feed_embedding=embedding, call_type="base_fed_eval")
        fetches = [self.total_base_fed_emb_loss]
        res = self.sess.run(fetches, feed_dict=feed_dict)
        return res

    def get_base_embedding(self, task):
        feed_dict = self.build_feed_dict(task, call_type="base_standard_eval")
        feed_dict[self.guess_input_mask_ph][:] = 1
        res = self.sess.run(self.guess_base_function_emb, feed_dict=feed_dict)
        return res

    def get_language_embedding(self, intified_task):
        feed_dict = self.build_feed_dict(task, call_type="base_lang_eval")
        res = self.sess.run(self.language_function_emb, feed_dict=feed_dict)
        return res

    def meta_class_loss_eval(self, meta_task):
        feed_dict = self.build_feed_dict(meta_task, call_type="meta_class_eval")
        return self.sess.run(self.total_meta_class_cached_emb_loss, feed_dict=feed_dict)
        
    def meta_map_loss_eval(self, meta_task):
        feed_dict = self.build_feed_dict(meta_task, call_type="meta_map_eval")
        return self.sess.run(self.total_meta_map_cached_emb_loss, feed_dict=feed_dict)

    def run_meta_loss_eval(self):
        names = []
        losses = []
        for meta_task, meta_class in zip([self.meta_class_train + self.meta_class_eval,
                                          self.meta_map_train + self.meta_map_eval],
                                         [True, False]:
            if meta_class:
                loss = self.meta_class_loss_eval(meta_task)
            else:
                loss = self.meta_map_loss_eval(meta_task)
            names.append(meta_task)
            losses.append(loss)

        return names, losses

    def get_meta_embedding(self, meta_task, meta_class):
        """Note: cached base embeddings must be up to date!"""
        call_type = "meta_class_eval" if meta_class else meta_map_eval
        feed_dict = self.build_feed_dict(meta_task, call_type=call_type)
        feed_dict[self.guess_input_mask_ph][:] = 1
        if meta_class:
            fetch = self.meta_class_guess_emb 
        else:
            fetch = self.meta_map_guess_emb 

        return self.sess.run(fetch, feed_dict=feed_dict)

    def meta_true_eval_step(self, meta_mapping):
        meta_dataset = self.meta_datasets[meta_mapping]["eval"]

        feed_dict = self.build_feed_dict(meta_mapping,
                                         call_type="metamap_cached_eval")
        result_embeddings = self.sess.run(self.meta_m_output,
                                          feed_dict=feed_dict) 
    
        names = []
        i = 0
        for train_or_eval in ["train", "eval"]:
            for (, (task, other)) in enumerate(
                    self.meta_pairings[meta_mapping][train_or_eval]):
                mapped_embedding = result_embeddings[i, :]
                names.append(meta_mapping + ":" + task + "->" + other)
                this_loss = self.base_embedding_eval(embedding=mapped_embedding, task=other)[0]
                losses.append(this_loss)
                i = i + 1
        return names, losses
    
    def run_meta_true_eval(self, include_new=False):
        """Evaluates true meta loss, i.e. the accuracy of the model produced
           by the embedding output by the meta task"""
        assert(False)  # again, must make sure that embeddings up to date if not persistent
        names = []
        losses = []
        for meta_mapping in self.meta_map_train + self.meta_map_eval:
            these_names, these_losses = self.meta_true_eval_step(meta_mapping) 
            names += these_names
            losses += these_losses

        return names, losses 

    def meta_class_train_step(self, meta_task, meta_lr):
        feed_dict = self.build_feed_dict(meta_task, lr=meta_lr, call_type="meta_class_train")
        self.sess.run(self.meta_class_train, feed_dict=feed_dict)

    def meta_map_train_step(self, meta_task, meta_lr):
        feed_dict = self.build_feed_dict(meta_task, lr=meta_lr, call_type="meta_map_train")
        self.sess.run(self.meta_map_train, feed_dict=feed_dict)

    def save_parameters(self, filename):
        self.saver.save(self.sess, filename)

    def restore_parameters(self, filename):
        self.saver.restore(self.sess, filename)

    def run_training(self):
        raise NotImplementedError("run_training() should be overridden by the child class!")

