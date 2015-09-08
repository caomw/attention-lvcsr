import logging
import numpy
import theano
from theano import tensor

from blocks.bricks import (
    Bias, Brick, Identity, Initializable, MLP, Linear, NDimensionalSoftmax,
    Sequence, Tanh)
from blocks.bricks.attention import SequenceContentAttention
from blocks.bricks.base import lazy, application
from blocks.bricks.parallel import Fork, Merge
from blocks.bricks.recurrent import (
    BaseRecurrent, Bidirectional,RecurrentStack, recurrent)
from blocks.bricks.sequence_generators import (
    AbstractEmitter, AbstractFeedback, SequenceGenerator, Readout,
    SoftmaxEmitter, LookupFeedback)
from blocks.bricks.wrappers import WithExtraDims
from blocks.graph import ComputationGraph
from blocks.filter import VariableFilter
from blocks.model import Model
from blocks.roles import OUTPUT
from blocks.search import BeamSearch
from blocks.serialization import load_parameter_values
from blocks.utils import dict_union, check_theano_variable

from lvsr.attention import SequenceContentAndConvAttention
from lvsr.ops import FST, FSTCostsOp, FSTTransitionOp, MAX_STATES, NOT_STATE
from lvsr.utils import global_push_initialization_config

logger = logging.getLogger(__name__)


class RecurrentWithFork(Initializable):

    @lazy(allocation=['input_dim'])
    def __init__(self, recurrent, input_dim, **kwargs):
        super(RecurrentWithFork, self).__init__(**kwargs)
        self.recurrent = recurrent
        self.input_dim = input_dim
        self.fork = Fork(
            [name for name in self.recurrent.sequences
             if name != 'mask'],
             prototype=Linear())
        self.children = [recurrent.brick, self.fork]

    def _push_allocation_config(self):
        self.fork.input_dim = self.input_dim
        self.fork.output_dims = [self.recurrent.brick.get_dim(name)
                                 for name in self.fork.output_names]

    @application(inputs=['input_', 'mask'])
    def apply(self, input_, mask=None, **kwargs):
        return self.recurrent(
            mask=mask, **dict_union(self.fork.apply(input_, as_dict=True),
                                    kwargs))

    @apply.property('outputs')
    def apply_outputs(self):
        return self.recurrent.states


class FSTTransition(BaseRecurrent, Initializable):
    def __init__(self, fst, remap_table, no_transition_cost, **kwargs):
        """Wrap FST in a recurrent brick.

        Parameters
        ----------
        fst : FST instance
        remap_table : dict
            Maps neutral network characters to FST characters.
        no_transition_cost : float
            Cost of going to the start state when no arc for an input
            symbol is available.

        """
        super(FSTTransition, self).__init__(**kwargs)
        self.fst = fst
        self.transition = FSTTransitionOp(fst, remap_table)
        self.probability_computer = FSTCostsOp(
            fst, remap_table, no_transition_cost)

        self.out_dim = len(remap_table)

    @recurrent(sequences=['inputs', 'mask'],
               states=['states', 'weights', 'add'],
               outputs=['states', 'weights', 'add'], contexts=[])
    def apply(self, inputs, states, weights, add,
              mask=None):
        new_states, new_weights = self.transition(states, weights, inputs)
        if mask:
            # In fact I don't really understand why we do this:
            # anyway states not covered by masks should have no effect
            # on the cost...
            new_states = tensor.cast(mask * new_states +
                                     (1. - mask) * states, 'int64')
            new_weights = mask * new_weights + (1. - mask) * weights
        new_add = self.probability_computer(new_states, new_weights)
        return new_states, new_weights, new_add

    @application(outputs=['states', 'weights', 'add'])
    def initial_states(self, batch_size, *args, **kwargs):
        states_dict = self.fst.expand({self.fst.fst.start: 0.0})
        states = tensor.as_tensor_variable(
            self.transition.pad(states_dict.keys(), NOT_STATE))
        states = tensor.tile(states[None, :], (batch_size, 1))
        weights = tensor.as_tensor_variable(
            self.transition.pad(states_dict.values(), 0))
        weights = tensor.tile(weights[None, :], (batch_size, 1))
        add = self.probability_computer(states, weights)
        return states, weights, add

    def get_dim(self, name):
        if name == 'states' or name == 'weights':
            return MAX_STATES
        if name == 'add':
            return self.out_dim
        if name == 'inputs':
            return 0
        return super(FSTTransition, self).get_dim(name)


class ShallowFusionReadout(Readout):
    def __init__(self, lm_costs_name, lm_weight,
                 normalize_am_weights=False,
                 normalize_lm_weights=False,
                 normalize_tot_weights=True,
                 am_beta=1.0,
                 **kwargs):
        super(ShallowFusionReadout, self).__init__(**kwargs)
        self.lm_costs_name = lm_costs_name
        self.lm_weight = lm_weight
        self.normalize_am_weights = normalize_am_weights
        self.normalize_lm_weights = normalize_lm_weights
        self.normalize_tot_weights = normalize_tot_weights
        self.am_beta = am_beta
        self.softmax = NDimensionalSoftmax()
        self.children += [self.softmax]

    @application
    def readout(self, **kwargs):
        lm_costs = -kwargs.pop(self.lm_costs_name)
        if self.normalize_lm_weights:
            lm_costs = self.softmax.log_probabilities(
                lm_costs, extra_ndim=lm_costs.ndim - 2)
        am_pre_softmax = self.am_beta * super(ShallowFusionReadout, self).readout(**kwargs)
        if self.normalize_am_weights:
            am_pre_softmax = self.softmax.log_probabilities(
                am_pre_softmax, extra_ndim=am_pre_softmax.ndim - 2)
        x = am_pre_softmax + self.lm_weight * lm_costs
        if self.normalize_tot_weights:
            x = self.softmax.log_probabilities(x, extra_ndim=x.ndim - 2)
        return x


class SelectInEachRow(Brick):
    @application(inputs=['matrix', 'indices'], outputs=['output_'])
    def apply(self, matrix, indices):
        return matrix[tensor.arange(matrix.shape[0]), indices]


class SelectInEachSubtensor(SelectInEachRow):
    decorators = [WithExtraDims()]


class LMEmitter(AbstractEmitter):
    """Emitter to use when language model is used.

    Since with the language model all normalization is
    done in ShallowFusionReadout, we need this no-op brick to
    interface it with BeamSearch.

    """
    @lazy(allocation=['readout_dim'])
    def __init__(self, readout_dim, **kwargs):
        super(LMEmitter, self).__init__(**kwargs)
        self.readout_dim = readout_dim
        self.select = SelectInEachSubtensor()
        self.children = [self.select]

    @application
    def emit(self, readouts):
        # Non-sense, but the returned result should never be used.
        return tensor.zeros_like(readouts[:, 0], dtype='int64')

    @application
    def cost(self, readouts, outputs):
        return -self.select.apply(
            readouts, outputs, extra_ndim=readouts.ndim - 2)

    @application
    def costs(self, readouts):
        return -readouts

    @application
    def initial_outputs(self, batch_size):
        # As long as we do not use the previous character, can be anything
        return tensor.zeros((batch_size,), dtype='int64')

    def get_dim(self, name):
        if name == 'outputs':
            return 0
        return super(LMEmitter, self).get_dim(name)


class InitializableSequence(Sequence, Initializable):
    pass


class Encoder(Initializable):

    def __init__(self, enc_transition, dims, dim_input, subsample, **kwargs):
        super(Encoder, self).__init__(**kwargs)
        self.subsample = subsample

        for layer_num, (dim_under, dim) in enumerate(
                zip([dim_input] + list(2 * numpy.array(dims)), dims)):
            bidir = Bidirectional(
                RecurrentWithFork(
                    enc_transition(dim=dim, activation=Tanh()).apply,
                    dim_under,
                    name='with_fork'),
                name='bidir{}'.format(layer_num))
            self.children.append(bidir)

    @application(outputs=['encoded', 'encoded_mask'])
    def apply(self, input_, mask=None):
        for bidir, take_each in zip(self.children, self.subsample):
            #No need to pad if all we do is subsample!
            #input_ = pad_to_a_multiple(input_, take_each, 0.)
            #if mask:
            #    mask = pad_to_a_multiple(mask, take_each, 0.)
            input_ = bidir.apply(input_, mask)
            input_ = input_[::take_each]
            if mask:
                mask = mask[::take_each]
        return input_, (mask if mask else tensor.ones_like(input_[:, :, 0]))


class OneOfNFeedback(AbstractFeedback, Initializable):
    """A feedback brick for the case when readout are integers.

    Stores and retrieves distributed representations of integers.

    """
    def __init__(self, num_outputs=None, feedback_dim=None, **kwargs):
        super(OneOfNFeedback, self).__init__(**kwargs)
        self.num_outputs = num_outputs
        self.feedback_dim = num_outputs

    @application
    def feedback(self, outputs):
        assert self.output_dim == 0
        eye = tensor.eye(self.num_outputs)
        check_theano_variable(outputs, None, "int")
        output_shape = [outputs.shape[i]
                        for i in range(outputs.ndim)] + [self.feedback_dim]
        return eye[outputs.flatten()].reshape(output_shape)

    def get_dim(self, name):
        if name == 'feedback':
            return self.feedback_dim
        return super(LookupFeedback, self).get_dim(name)


class SpeechModel(Model):
    def set_parameter_values(self, param_values):
        filtered_param_values = {
            key: value for key, value in param_values.items()
            # Shared variables are now saved separately, thanks to the
            # recent PRs by Dmitry Serdyuk and Bart. Unfortunately,
            # that applies to all shared variables, and not only to the
            # parameters. That's why temporarily we have to filter the
            # unnecessary ones. The filter deliberately does not take into
            # account for a few exotic ones, there will be a warning
            # with the list of the variables that were not matched with
            # model parameters.
            if not ('shared' in key
                    or 'None' in key)}
        super(SpeechModel,self).set_parameter_values(filtered_param_values)


class SpeechRecognizer(Initializable):
    """Encapsulate all reusable logic.

    This class plays a few roles: (a) it's a top brick that knows
    how to combine bottom, bidirectional and recognizer network, (b)
    it has the inputs variables and can build whole computation graphs
    starting with them (c) it hides compilation of Theano functions
    and initialization of beam search. I find it simpler to have it all
    in one place for research code.

    Parameters
    ----------
    All defining the structure and the dimensions of the model. Typically
    receives everything from the "net" section of the config.

    """
    def __init__(self, recordings_source, labels_source, eos_label,
                 num_features, num_phonemes,
                 dim_dec, dims_bidir, dims_bottom,
                 enc_transition, dec_transition,
                 use_states_for_readout,
                 attention_type,
                 lm=None, character_map=None,
                 subsample=None,
                 dims_top=None,
                 prior=None, conv_n=None,
                 bottom_activation=None,
                 post_merge_activation=None,
                 post_merge_dims=None,
                 dim_matcher=None,
                 embed_outputs=True,
                 dec_stack=1,
                 conv_num_filters=1,
                 data_prepend_eos=True,
                 energy_normalizer=None,  # softmax is th edefault set in SequenceContentAndConvAttention
                 **kwargs):
        if bottom_activation is None:
            bottom_activation = Tanh()
        if post_merge_activation is None:
            post_merge_activation = Tanh()
        super(SpeechRecognizer, self).__init__(**kwargs)
        self.recordings_source = recordings_source
        self.labels_source = labels_source
        self.eos_label = eos_label
        self.data_prepend_eos = data_prepend_eos

        self.rec_weights_init = None
        self.initial_states_init = None

        self.enc_transition = enc_transition
        self.dec_transition = dec_transition
        self.dec_stack = dec_stack

        bottom_activation = bottom_activation
        post_merge_activation = post_merge_activation

        if dim_matcher is None:
            dim_matcher = dim_dec

        # The bottom part, before BiRNN
        if dims_bottom:
            bottom = MLP([bottom_activation] * len(dims_bottom),
                         [num_features] + dims_bottom,
                         name="bottom")
        else:
            bottom = Identity(name='bottom')

        # BiRNN
        if not subsample:
            subsample = [1] * len(dims_bidir)
        encoder = Encoder(self.enc_transition, dims_bidir,
                          dims_bottom[-1] if len(dims_bottom) else num_features,
                          subsample)

        # The top part, on top of BiRNN but before the attention
        if dims_top:
            top = MLP([Tanh()],
                      [2 * dims_bidir[-1]] + dims_top + [2 * dims_bidir[-1]], name="top")
        else:
            top = Identity(name='top')

        if dec_stack == 1:
            transition = self.dec_transition(
                dim=dim_dec, activation=Tanh(), name="transition")
        else:
            transitions = [self.dec_transition(dim=dim_dec,
                                               activation=Tanh(),
                                               name="transition_{}".format(trans_level))
                           for trans_level in xrange(dec_stack)]
            transition = RecurrentStack(transitions=transitions,
                                        skip_connections=True)
        # Choose attention mechanism according to the configuration
        if attention_type == "content":
            attention = SequenceContentAttention(
                state_names=transition.apply.states,
                attended_dim=2 * dims_bidir[-1], match_dim=dim_matcher,
                name="cont_att")
        elif attention_type == "content_and_conv":
            attention = SequenceContentAndConvAttention(
                state_names=transition.apply.states,
                conv_n=conv_n,
                conv_num_filters=conv_num_filters,
                attended_dim=2 * dims_bidir[-1], match_dim=dim_matcher,
                prior=prior,
                energy_normalizer=energy_normalizer,
                name="conv_att")
        else:
            raise ValueError("Unknown attention type {}"
                             .format(attention_type))
        if embed_outputs:
            feedback = LookupFeedback(num_phonemes + 1, dim_dec)
        else:
            feedback = OneOfNFeedback(num_phonemes + 1)
        if lm:
            # In case we use LM it is Readout that is responsible
            # for normalization.
            emitter = LMEmitter()
        else:
            emitter = SoftmaxEmitter(initial_output=num_phonemes, name="emitter")
        readout_config = dict(
            readout_dim=num_phonemes,
            source_names=(transition.apply.states if use_states_for_readout else [])
                         + [attention.take_glimpses.outputs[0]],
            emitter=emitter,
            feedback_brick=feedback,
            name="readout")
        if post_merge_dims:
            readout_config['merged_dim'] = post_merge_dims[0]
            readout_config['post_merge'] = InitializableSequence([
                Bias(post_merge_dims[0]).apply,
                post_merge_activation.apply,
                MLP([post_merge_activation] * (len(post_merge_dims) - 1) + [Identity()],
                    # MLP was designed to support Maxout is activation
                    # (because Maxout in a way is not one). However
                    # a single layer Maxout network works with the trick below.
                    # For deeper Maxout network one has to use the
                    # Sequence brick.
                    [d//getattr(post_merge_activation, 'num_pieces', 1)
                     for d in post_merge_dims] + [num_phonemes]).apply,
            ],
                name='post_merge')
        readout = Readout(**readout_config)

        language_model = None
        if lm:
            lm_weight = lm.pop('weight', 0.0)
            normalize_am_weights = lm.pop('normalize_am_weights', True)
            normalize_lm_weights = lm.pop('normalize_lm_weights', False)
            normalize_tot_weights = lm.pop('normalize_tot_weights', False)
            am_beta = lm.pop('am_beta', 1.0)
            if normalize_am_weights + normalize_lm_weights + normalize_tot_weights < 1:
                logger.warn("Beam search is prone to fail with no log-prob normalization")
            language_model = LanguageModel(nn_char_map=character_map, **lm)
            readout = ShallowFusionReadout(lm_costs_name='lm_add',
                                           lm_weight=lm_weight,
                                           normalize_am_weights=normalize_am_weights,
                                           normalize_lm_weights=normalize_lm_weights,
                                           normalize_tot_weights=normalize_tot_weights,
                                           am_beta=am_beta,
                                           **readout_config)

        generator = SequenceGenerator(
            readout=readout, transition=transition, attention=attention,
            language_model=language_model,
            name="generator")

        # Remember child bricks
        self.encoder = encoder
        self.bottom = bottom
        self.top = top
        self.generator = generator
        self.children = [encoder, top, bottom, generator]

        # Create input variables
        self.recordings = tensor.tensor3(self.recordings_source)
        self.recordings_mask = tensor.matrix(self.recordings_source + "_mask")
        self.labels = tensor.lmatrix(self.labels_source)
        self.labels_mask = tensor.matrix(self.labels_source + "_mask")
        self.batch_inputs = [self.recordings, self.recordings_source,
                             self.labels, self.labels_mask]
        self.single_recording = tensor.matrix(self.recordings_source)
        self.single_transcription = tensor.lvector(self.labels_source)

    def push_initialization_config(self):
        super(SpeechRecognizer, self).push_initialization_config()
        if self.rec_weights_init:
            rec_weights_config = {'weights_init': self.rec_weights_init,
                                  'recurrent_weights_init': self.rec_weights_init}
            global_push_initialization_config(self,
                                              rec_weights_config,
                                              BaseRecurrent)
        if self.initial_states_init:
            global_push_initialization_config(self,
                                              {'initial_states_init': self.initial_states_init})

    @application
    def cost(self, recordings, recordings_mask, labels, labels_mask):
        bottom_processed = self.bottom.apply(recordings)
        encoded, encoded_mask = self.encoder.apply(
            input_=bottom_processed,
            mask=recordings_mask)
        encoded = self.top.apply(encoded)
        return self.generator.cost_matrix(
            labels, labels_mask,
            attended=encoded, attended_mask=encoded_mask)

    @application
    def generate(self, recordings):
        encoded, encoded_mask = self.encoder.apply(
            input_=self.bottom.apply(recordings))
        encoded = self.top.apply(encoded)
        return self.generator.generate(
            n_steps=recordings.shape[0], batch_size=recordings.shape[1],
            attended=encoded,
            attended_mask=encoded_mask,
            as_dict=True)

    def load_params(self, path):
        generated = self.get_generate_graph()
        param_values = load_parameter_values(path)
        SpeechModel(generated['outputs']).set_parameter_values(param_values)

    def get_generate_graph(self):
        result = self.generate(self.recordings)
        return result

    def get_cost_graph(self, batch=True):
        if batch:
            return self.cost(
                self.recordings, self.recordings_mask,
                self.labels, self.labels_mask)
        recordings = self.single_recording[:, None, :]
        labels = self.single_transcription[:, None]
        return self.cost(
            recordings, tensor.ones_like(recordings[:, :, 0]),
            labels, None)

    def analyze(self, recording, transcription):
        """Compute cost and aligment for a recording/transcription pair."""
        if not hasattr(self, "_analyze"):
            cost = self.get_cost_graph(batch=False)
            cg = ComputationGraph(cost)
            energies = VariableFilter(
                bricks=[self.generator], name="energies")(cg)
            energies_output = [energies[0][:, 0, :] if energies
                               else tensor.zeros((self.single_transcription.shape[0],
                                                  self.single_recording.shape[0]))]
            states, = VariableFilter(
                applications=[self.encoder.apply], roles=[OUTPUT],
                name="encoded")(cg)
            ctc_matrix_output = []
            if len(self.generator.readout.source_names) == 1:
                ctc_matrix_output = [
                    self.generator.readout.readout(weighted_averages=states)[:, 0, :]]
            weights, = VariableFilter(
                bricks=[self.generator], name="weights")(cg)
            self._analyze = theano.function(
                [self.single_recording, self.single_transcription],
                [cost[:, 0], weights[:, 0, :]] + energies_output + ctc_matrix_output)
        return self._analyze(recording, transcription)

    def init_beam_search(self, beam_size):
        """Compile beam search and set the beam size.

        See Blocks issue #500.

        """
        self.beam_size = beam_size
        generated = self.get_generate_graph()
        samples, = VariableFilter(
            applications=[self.generator.generate], name="outputs")(
            ComputationGraph(generated['outputs']))
        self._beam_search = BeamSearch(beam_size, samples)
        self._beam_search.compile()

    def beam_search(self, recording, char_discount=0.0):
        if not hasattr(self, '_beam_search'):
            self.init_beam_search(self.beam_size)
        input_ = recording[:,numpy.newaxis,:]
        outputs, search_costs = self._beam_search.search(
            {self.recordings: input_}, self.eos_label, input_.shape[0] / 3,
            ignore_first_eol=self.data_prepend_eos,
            char_discount=char_discount)
        return outputs, search_costs

    def __getstate__(self):
        state = dict(self.__dict__)
        for attr in ['_analyze', '_beam_search']:
            state.pop(attr, None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        # To use bricks used on a GPU first on a CPU later
        emitter = self.generator.readout.emitter
        if hasattr(emitter, '_theano_rng'):
            del emitter._theano_rng


class LanguageModel(SequenceGenerator):

    def __init__(self, type_, path, nn_char_map, no_transition_cost=1e12, **kwargs):
        # TODO: num_labels should be possible to extract from the FST
        if type_ != 'fst':
            raise ValueError("Supports only FST's so far.")
        fst = FST(path)
        fst_char_map = dict(fst.fst.isyms.items())
        del fst_char_map['<eps>']
        if not len(fst_char_map) == len(nn_char_map):
            raise ValueError()
        remap_table = {nn_char_map[character]: fst_code
                       for character, fst_code in fst_char_map.items()}
        transition = FSTTransition(fst, remap_table, no_transition_cost)

        # This SequenceGenerator will be used only in a very limited way.
        # That's why it is sufficient to equip it with a completely
        # fake readout.
        dummy_readout = Readout(
            source_names=['add'], readout_dim=len(remap_table),
            merge=Merge(input_names=['costs'], prototype=Identity()),
            post_merge=Identity(),
            emitter=SoftmaxEmitter())
        super(LanguageModel, self).__init__(
            transition=transition,
            fork=Fork(output_names=[name for name in transition.apply.sequences
                                    if name != 'mask'],
                      prototype=Identity()),
            readout=dummy_readout, **kwargs)
