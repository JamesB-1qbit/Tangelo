# Copyright 2021 Good Chemistry Company.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Simulator class, wrapping around the various simulators and abstracting their
differences from the user. Able to run noiseless and noisy simulations,
leveraging the capabilities of different backends, quantum or classical.

If the user provides a noise model, then a noisy simulation is run with n_shots
shots. If the user only provides n_shots, a noiseless simulation is run, drawing
the desired amount of shots. If the target backend has access to the statevector
representing the quantum state, we leverage any kind of emulation available to
reduce runtime (directly generating shot values from final statevector etc) If
the quantum circuit contains a MEASURE instruction, it is assumed to simulate a
mixed-state and the simulation will be carried by simulating individual shots
(e.g a number of shots is required).

Some backends may only support a subset of the above. This information is
contained in a separate data-structure.
"""

import abc
import math
from collections import Counter

import numpy as np
from scipy import stats
from bitarray import bitarray
from openfermion.ops import QubitOperator

from tangelo.helpers.utils import default_simulator, installed_simulator
from tangelo.linq import Gate, Circuit
from tangelo.linq.helpers.circuits.measurement_basis import measurement_basis_gates


class SimulatorBase(abc.ABC):

    def __init__(self, n_shots=None, noise_model=None, backend_info=None):
        """Instantiate Simulator object.

        Args:
            n_shots (int): Number of shots if using a shot-based simulator.
            noise_model: A noise model object assumed to be in the format
                expected from the target backend.
        """
        self._source = "abstract"
        self._current_state = None
        self._noise_model = noise_model

        # Can be modified later by user as long as long as it retains the same type (ex: cannot change to/from None)
        self.n_shots = n_shots
        self.freq_threshold = 1e-10

        # Set additional attributes related to the target backend chosen by the user
        for k, v in backend_info.items():
            setattr(self, k, v)

        # Raise error if user attempts to pass a noise model to a backend not supporting noisy simulation
        if self._noise_model and not self.noisy_simulation:
            raise ValueError("Target backend does not support noise models.")

        # Raise error if the number of shots has not been passed for a noisy simulation or if statevector unavailable
        if not self.n_shots and (not self.statevector_available or self._noise_model):
            raise ValueError("A number of shots needs to be specified.")

    @abc.abstractmethod
    def simulate_circuit(self):
        """Perform state preparation corresponding to the input circuit on the
        target backend, return the frequencies of the different observables, and
        either the statevector or None depending on the availability of the
        statevector and if return_statevector is set to True. For the
        statevector backends supporting it, an initial statevector can be
        provided to initialize the quantum state without simulating all the
        equivalent gates.

        Args:
            source_circuit: a circuit in the abstract format to be translated
                for the target backend.
            return_statevector(bool): option to return the statevector as well,
                if available.
            initial_statevector(list/array) : A valid statevector in the format
                supported by the target backend.

        Returns:
            dict: A dictionary mapping multi-qubit states to their corresponding
                frequency.
            numpy.array: The statevector, if available for the target backend
                and requested by the user (if not, set to None).
        """
        pass

    def simulate(self, source_circuit, return_statevector=False, initial_statevector=None):
        """Perform state preparation corresponding to the input circuit on the
        target backend, return the frequencies of the different observables, and
        either the statevector or None depending on the availability of the
        statevector and if return_statevector is set to True. For the
        statevector backends supporting it, an initial statevector can be
        provided to initialize the quantum state without simulating all the
        equivalent gates.

        Args:
            source_circuit: a circuit in the abstract format to be translated
                for the target backend.
            return_statevector(bool): option to return the statevector as well,
                if available.
            initial_statevector(list/array) : A valid statevector in the format
                supported by the target backend.

        Returns:
            dict: A dictionary mapping multi-qubit states to their corresponding
                frequency.
            numpy.array: The statevector, if available for the target backend
                and requested by the user (if not, set to None).
        """

        if source_circuit.is_mixed_state and not self.n_shots:
            raise ValueError("Circuit contains MEASURE instruction, and is assumed to prepare a mixed state."
                             "Please set the Simulator.n_shots attribute to an appropriate value.")

        if source_circuit.width == 0:
            raise ValueError("Cannot simulate an empty circuit (e.g identity unitary) with unknown number of qubits.")

        # If the unitary is the identity (no gates) and no noise model, no need for simulation:
        # return all-zero state or sample from statevector
        if source_circuit.size == 0 and not self._noise_model:
            if initial_statevector is not None:
                statevector = initial_statevector
                frequencies = self._statevector_to_frequencies(initial_statevector)
            else:
                frequencies = {'0'*source_circuit.width: 1.0}
                statevector = np.zeros(2**source_circuit.width)
                statevector[0] = 1.0
            return (frequencies, statevector) if return_statevector else (frequencies, None)

        return self.simulate_circuit(source_circuit, return_statevector=return_statevector, initial_statevector=initial_statevector)

    def get_expectation_value(self, qubit_operator, state_prep_circuit, initial_statevector=None):
        r"""Take as input a qubit operator H and a quantum circuit preparing a
        state |\psi>. Return the expectation value <\psi | H | \psi>.

        In the case of a noiseless simulation, if the target backend exposes the
        statevector then it is used directly to compute expectation values, or
        draw samples if required. In the case of a noisy simulator, or if the
        statevector is not available on the target backend, individual shots
        must be run and the workflow is akin to what we would expect from an
        actual QPU.

        Args:
            qubit_operator(openfermion-style QubitOperator class): a qubit
                operator.
            state_prep_circuit: an abstract circuit used for state preparation.

        Returns:
            complex: The expectation value of this operator with regards to the
                state preparation.
        """
        # Check if simulator supports statevector
        if initial_statevector is not None and not self.statevector_available:
            raise ValueError(f'Statevector not supported in {self.__class__}')

        # Check that qubit operator does not operate on qubits beyond circuit size.
        # Keep track if coefficients are real or not
        are_coefficients_real = True
        for term, coef in qubit_operator.terms.items():
            if state_prep_circuit.width < len(term):
                raise ValueError(f'Term {term} requires more qubits than the circuit contains ({state_prep_circuit.width})')
            if type(coef) in {complex, np.complex64, np.complex128}:
                are_coefficients_real = False

        # If the underlying operator is hermitian, expectation value is real and can be computed right away
        if are_coefficients_real:
            if self._noise_model or not self.statevector_available \
                    or state_prep_circuit.is_mixed_state or state_prep_circuit.size == 0:
                return self._get_expectation_value_from_frequencies(qubit_operator, state_prep_circuit, initial_statevector=initial_statevector)
            elif self.statevector_available:
                return self._get_expectation_value_from_statevector(qubit_operator, state_prep_circuit, initial_statevector=initial_statevector)

        # Else, separate the operator into 2 hermitian operators, use linearity and call this function twice
        else:
            qb_op_real, qb_op_imag = QubitOperator(), QubitOperator()
            for term, coef in qubit_operator.terms.items():
                qb_op_real.terms[term], qb_op_imag.terms[term] = coef.real, coef.imag
            qb_op_real.compress()
            qb_op_imag.compress()
            exp_real = self.get_expectation_value(qb_op_real, state_prep_circuit, initial_statevector=initial_statevector)
            exp_imag = self.get_expectation_value(qb_op_imag, state_prep_circuit, initial_statevector=initial_statevector)
            return exp_real if (exp_imag == 0.) else exp_real + 1.0j * exp_imag

    def _get_expectation_value_from_statevector(self, qubit_operator, state_prep_circuit, initial_statevector=None):
        r"""Take as input a qubit operator H and a state preparation returning a
        ket |\psi>. Return the expectation value <\psi | H | \psi>, computed
        without drawing samples (statevector only). Users should not be calling
        this function directly, please call "get_expectation_value" instead.

        Args:
            qubit_operator(openfermion-style QubitOperator class): a qubit
                operator.
            state_prep_circuit: an abstract circuit used for state preparation
                (only pure states).

        Returns:
            complex: The expectation value of this operator with regards to the
                state preparation.
        """
        n_qubits = state_prep_circuit.width

        expectation_value = 0.
        prepared_frequencies, prepared_state = self.simulate(state_prep_circuit, return_statevector=True, initial_statevector=initial_statevector)

        if hasattr(self, "expectation_value_from_prepared_state"):
            return self.expectation_value_from_prepared_state(qubit_operator, n_qubits, prepared_state)

        # Otherwise, use generic statevector expectation value
        for term, coef in qubit_operator.terms.items():

            if len(term) > n_qubits:  # Cannot have a qubit index beyond circuit size
                raise ValueError(f"Size of operator {qubit_operator} beyond circuit width ({n_qubits} qubits)")
            elif not term:  # Empty term: no simulation needed
                expectation_value += coef
                continue

            if not self.n_shots:
                # Directly simulate and compute expectation value using statevector
                pauli_circuit = Circuit([Gate(pauli, index) for index, pauli in term], n_qubits=n_qubits)
                _, pauli_state = self.simulate(pauli_circuit, return_statevector=True, initial_statevector=prepared_state)

                delta = np.dot(pauli_state.real, prepared_state.real) + np.dot(pauli_state.imag, prepared_state.imag)
                expectation_value += coef * delta

            else:
                # Run simulation with statevector but compute expectation value with samples directly drawn from it
                basis_circuit = Circuit(measurement_basis_gates(term), n_qubits=state_prep_circuit.width)
                if basis_circuit.size > 0:
                    frequencies, _ = self.simulate(basis_circuit, initial_statevector=prepared_state)
                else:
                    frequencies = prepared_frequencies
                expectation_term = self.get_expectation_value_from_frequencies_oneterm(term, frequencies)
                expectation_value += coef * expectation_term

        return expectation_value

    def _get_expectation_value_from_frequencies(self, qubit_operator, state_prep_circuit, initial_statevector=None):
        r"""Take as input a qubit operator H and a state preparation returning a
        ket |\psi>. Return the expectation value <\psi | H | \psi> computed
        using the frequencies of observable states.

        Args:
            qubit_operator(openfermion-style QubitOperator class): a qubit
                operator.
            state_prep_circuit: an abstract circuit used for state preparation.

        Returns:
            complex: The expectation value of this operator with regards to the
                state preparation.
        """
        n_qubits = state_prep_circuit.width
        if not self.statevector_available or state_prep_circuit.is_mixed_state or self._noise_model:
            initial_circuit = state_prep_circuit
            if initial_statevector is not None and not self.statevector_available:
                raise ValueError(f'Backend {self.__class__} does not support statevectors')
            else:
                updated_statevector = initial_statevector
        else:
            initial_circuit = Circuit(n_qubits=n_qubits)
            _, updated_statevector = self.simulate(state_prep_circuit, return_statevector=True, initial_statevector=initial_statevector)

        expectation_value = 0.
        for term, coef in qubit_operator.terms.items():

            if len(term) > n_qubits:
                raise ValueError(f"Size of operator {qubit_operator} beyond circuit width ({n_qubits} qubits)")
            elif not term:  # Empty term: no simulation needed
                expectation_value += coef
                continue

            basis_circuit = Circuit(measurement_basis_gates(term))
            full_circuit = initial_circuit + basis_circuit if (basis_circuit.size > 0) else initial_circuit
            frequencies, _ = self.simulate(full_circuit, initial_statevector=updated_statevector)
            expectation_term = self.get_expectation_value_from_frequencies_oneterm(term, frequencies)
            expectation_value += coef * expectation_term

        return expectation_value

    @staticmethod
    def get_expectation_value_from_frequencies_oneterm(term, frequencies):
        """Return the expectation value of a single-term qubit-operator, given
        the result of a state-preparation.

        Args:
            term(openfermion-style QubitOperator object): a qubit operator, with
                only a single term.
            frequencies(dict): histogram of frequencies of measurements (assumed
                to be in lsq-first format).

        Returns:
            complex: The expectation value of this operator with regards to the
                state preparation.
        """

        if not frequencies.keys():
            return ValueError("Must pass a non-empty dictionary of frequencies.")
        n_qubits = len(list(frequencies.keys())[0])

        # Get term mask
        mask = ["0"] * n_qubits
        for index, op in term:
            mask[index] = "1"
        mask = "".join(mask)

        # Compute expectation value of the term
        expectation_term = 0.
        for basis_state, freq in frequencies.items():
            # Compute sample value using state_binstr and term mask, update term expectation value
            sample = (-1) ** ((bitarray(mask) & bitarray(basis_state)).to01().count("1") % 2)
            expectation_term += sample * freq

        return expectation_term

    def _statevector_to_frequencies(self, statevector):
        """For a given statevector representing the quantum state of a qubit
        register, returns a sparse histogram of the probabilities in the
        least-significant-qubit (lsq) -first order. e.g the string '100' means
        qubit 0 measured in basis state |1>, and qubit 1 & 2 both measured in
        state |0>.

        Args:
            statevector(list or ndarray(complex)): an iterable 1D data-structure
                containing the amplitudes.

        Returns:
            dict: A dictionary whose keys are bitstrings representing the
                multi-qubit states with the least significant qubit first (e.g
                '100' means qubit 0 in state |1>, and qubit 1 and 2 in state
                |0>), and the associated value is the corresponding frequency.
                Unless threshold=0., this dictionary will be sparse.
        """

        n_qubits = int(math.log2(len(statevector)))
        frequencies = dict()
        for i, amplitude in enumerate(statevector):
            frequency = abs(amplitude)**2
            if (frequency - self.freq_threshold) >= 0.:
                frequencies[self._int_to_binstr(i, n_qubits)] = frequency

        # If n_shots, has been specified, then draw that amount of samples from the distribution
        # and return empirical frequencies instead. Otherwise, return the exact frequencies
        if not self.n_shots:
            return frequencies
        else:
            xk, pk = [], []
            for k, v in frequencies.items():
                xk.append(int(k[::-1], 2))
                pk.append(frequencies[k])
            distr = stats.rv_discrete(name='distr', values=(np.array(xk), np.array(pk)))

            # Generate samples from distribution. Cut in chunks to ensure samples fit in memory, gradually accumulate
            chunk_size = 10**7
            n_chunks = self.n_shots // chunk_size
            freqs_shots = Counter()

            for i in range(n_chunks+1):
                this_chunk = self.n_shots % chunk_size if i == n_chunks else chunk_size
                samples = distr.rvs(size=this_chunk)
                freqs_shots += Counter(samples)
            freqs_shots = {self._int_to_binstr_lsq(k, n_qubits): v / self.n_shots for k, v in freqs_shots.items()}
            return freqs_shots

    def _int_to_binstr(self, i, n_qubits):
        """Convert an integer into a bit string of size n_qubits, in the order
        specified for the state vector.
        """
        bs = bin(i).split('b')[-1]
        state_binstr = "0" * (n_qubits - len(bs)) + bs
        return state_binstr[::-1] if (self.statevector_order == "msq_first") else state_binstr

    def _int_to_binstr_lsq(self, i, n_qubits):
        """Convert an integer into a bit string of size n_qubits, in the
        least-significant qubit order.
        """
        bs = bin(i).split('b')[-1]
        state_binstr = "0" * (n_qubits - len(bs)) + bs
        return state_binstr[::-1]

    @property
    def backend_info(self):
        """A dictionary that includes {'noisy_simulation': True or False,
                                       'statevector_available': True or False,
                                       'statevector_order': 'lsq_first' or 'msq_first'"""
        pass


def Simulator(target=default_simulator, n_shots=None, noise_model=None, **kwargs) -> SimulatorBase:
    """Return requested target simulator

    Args:
        target (string or BaseSimulator): String can be "qiskit", "cirq", "qdk" or "qulacs". Can also provide
            The child class of BaseSimulator.
        n_shots (int): Number of shots if using a shot-based simulator.
        noise_model: A noise model object assumed to be in the format
            expected from the target backend.

    Returns:
        BaseSimulator: The initialized target simulator that is a child class of BaseSimulator.
    """
    if target is None:
        target = default_simulator
    if isinstance(target, str):
        from tangelo.linq.target import Cirq, Qulacs, Qiskit, QDK, QSimCirq
        target_dict = {"qiskit": Qiskit, "cirq": Cirq, "qdk": QDK, "qulacs": Qulacs, "qsimcirq": QSimCirq}
        simulator = target_dict[target](n_shots=n_shots, noise_model=noise_model)
    else:
        simulator = target(n_shots=n_shots, noise_model=noise_model, **kwargs)

    return simulator


# Generate backend info dictionary
def get_backend_info():
    """Return backend info for each installed backend"""
    backend_info = dict()
    for sim_id in installed_simulator:
        sim_id_sim = Simulator(sim_id, n_shots=1)
        backend_info[sim_id] = sim_id_sim.backend_info
    return backend_info


backend_info = get_backend_info()
