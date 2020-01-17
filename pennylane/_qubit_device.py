# Copyright 2018-2020 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This module contains the :class:`QubitDevice` abstract base class.
"""

# For now, arguments may be different from the signatures provided in Device
# e.g. instead of expval(self, observable, wires, par) have expval(self, observable)
# pylint: disable=arguments-differ, abstract-method, no-value-for-parameter,too-many-instance-attributes
import abc

import numpy as np

from pennylane.operation import Sample, Variance, Expectation, Probability
from pennylane.qnodes import QuantumFunctionError
from pennylane import Device


class QubitDevice(Device):
    """Abstract base class for PennyLane qubit devices.

    The following abstract methods **must** be defined:

    * :meth:`~.probability`: returns the probability or marginal probability from the
      device after circuit execution. :meth:`~.marginal_prob` may be used here.

    * :meth:`~.apply`: append circuit operations, compile the circuit (if applicable),
      and perform the quantum computation.

    Where relevant, devices that generate their own samples (such as hardware) should
    overwrite the following methods:

    * :meth:`~.generate_samples`: Generate samples from the device from the
      exact or approximate probability distribution.

    This device contains common utility methods for qubit-based devices. These
    do not need to be overwritten. Utility methods include:

    * :meth:`~.expval`, :meth:`~.var`, :meth:`~.sample`: return expectation values,
      variances, and samples of observables after the circuit has been rotated
      into the observable eigenbasis.

    Args:
        wires (int): number of subsystems in the quantum state represented by the device
        shots (int): number of circuit evaluations/random samples used to estimate
            expectation values of observables
        analytic (bool): If ``True``, the device calculates probability, expectation values,
            and variances analytically. If ``False``, a finite number of samples set by
            the argument ``shots`` are used to estimate these quantities.
    """

    # pylint: disable=too-many-public-methods
    _asarray = staticmethod(np.asarray)

    def __init__(self, wires=1, shots=1000, analytic=True):
        super().__init__(wires=wires, shots=shots)

        self.analytic = analytic
        """bool: If ``True``, the device supports exact calculation of expectation
        values, variance, and probabilities. If ``False``, samples are used
        to estimate the statistical quantities above."""

        self._wires_used = set()
        """set[int]: wires acted on by quantum operations and observables"""

        self._memory = False
        """bool: if True, indicates to the device that samples should be returned.
        This is required if samples are being returned by the QNode, or if the device is in
        non-analytic mode."""

        self._samples = None
        """None or array[int]: If :attr:`~._memory` is True, stores the samples
        generated by the device *after* rotation to diagonalize the observables."""

    def reset(self):
        """Reset the backend state.

        After the reset, the backend should be as if it was just constructed.
        Most importantly the quantum state is reset to its initial value.
        """
        self._wires_used = set()
        self._memory = False
        self._samples = None

    def execute(self, circuit):
        """Execute a queue of quantum operations on the device and then
        measure the given observables.

        For plugin developers: instead of overwriting this, consider
        implementing a suitable subset of

        * :meth:`apply`

        * :meth:`~.generate_samples`

        * :meth:`~.probability`

        Args:
            circuit (~.CircuitGraph): circuit to execute on the device

        Raises:
            QuantumFunctionError: if the value of :attr:`~.Observable.return_type` is not supported

        Returns:
            array[float]: measured value(s)
        """
        self.check_validity(circuit.operations, circuit.observables)

        with self.execution_context():
            # determine operations required to diagonalize observables
            rotations = self.rotate_basis(circuit.observables)

            # apply all circuit operations
            self.apply(circuit.operations, rotations)

            # generate computational basis samples
            self.generate_samples()

            # compute the required statistics
            results = self.statistics(circuit.observables)

            # Ensures that a combination with sample does not put
            # expvals and vars in superfluous arrays
            sample_return_types = (obs.return_type is Sample for obs in circuit.observables)
            if any(sample_return_types) and not all(sample_return_types):
                return self._asarray(results, dtype="object")

            return self._asarray(results)

    @abc.abstractmethod
    def apply(self, operations, rotations=None, **kwargs):
        """Apply quantum operations, and execute the quantum circuit.

        Args:
            operations (list[~.Operation]): operations to apply to the device
            rotations (list[~.Operation]): operations that rotate the circuit
                pre-measurement into the eigenbasis of the observables.
        """

    def rotate_basis(self, observables):
        """Rotates the specified wires such that they
        are in the eigenbasis of the provided observables.

        Args:
            observables (List[~.Observable]): the observables to diagonalize

        Returns:
            List[~.Operation]: the operations that diagonalize the observables
        """
        wires = []
        rotation_gates = []

        for observable in observables:
            if hasattr(observable, "return_type") and observable.return_type == Sample:
                self._memory = True  # make sure to return samples

            rotation_gates.extend(observable.diagonalizing_gates())

            for wire in observable.wires:
                if isinstance(wire, int):
                    wires.append(wire)
                else:
                    wires.extend(wire)

        # Store the wires used by the observables such that
        # an Identity is considered on the remaining wires
        self._wires_used = set(wires)

        return rotation_gates

    def statistics(self, observables):
        """Process measurement results from circuit execution and return statistics.

        This includes returning expectation values, variance, samples and probabilities.

        Args:
            observables (List[:class:`Observable`]): the observables to be measured

        Raises:
            QuantumFunctionError: if the value of :attr:`~.Observable.return_type` is not supported

        Returns:
            Union[float, List[float]]: the corresponding statistics
        """
        results = []

        for obs in observables:
            # Pass instances directly
            if obs.return_type is Expectation:
                results.append(self.expval(obs))

            elif obs.return_type is Variance:
                results.append(self.var(obs))

            elif obs.return_type is Sample:
                results.append(np.array(self.sample(obs)))

            elif obs.return_type is Probability:
                results.append(self.probability(wires=obs.wires))

            elif obs.return_type is not None:
                raise QuantumFunctionError(
                    "Unsupported return type specified for observable {}".format(obs.name)
                )

        return results

    def generate_samples(self):
        """Generate computational basis samples.

        If the device contains a sample return type, or the
        device is running in non-analytic mode, ``dev.shots`` number of
        computational basis samples are generated and stored within
        the :attr:`~._samples` attribute.

        .. warning::

            This method should be overwritten on devices that
            generate their own computational basis samples.
        """
        number_of_states = 2 ** len(self._wires_used)
        rotated_prob = self.probability(self._wires_used)
        samples = self.sample_basis_states(number_of_states, rotated_prob)
        self._samples = QubitDevice.states_to_binary(samples, number_of_states)

    def sample_basis_states(self, number_of_states, state_probability):
        """Sample from the computational basis states based on the state
        probability.

        This is an auxiliary method to the generate_samples method.

        Args:
            number_of_states (int): the number of basis states to sample from

        Returns:
            List[int]: the sampled basis states
        """
        basis_states = np.arange(number_of_states)
        return np.random.choice(basis_states, self.shots, p=state_probability)

    @staticmethod
    def states_to_binary(samples, number_of_states):
        """Convert basis states from base 10 to binary representation.

        This is an auxiliary method to the generate_samples method.

        Args:
            samples (List[int]): samples of basis states in base 10 representation
            number_of_states (int): the number of basis states to sample from

        Returns:
            List[int]: basis states in binary representation
        """
        powers_of_two = 1 << np.arange(number_of_states)
        states_sampled_base_ten = samples[:, None] & powers_of_two
        return (states_sampled_base_ten > 0).astype(int)

    @abc.abstractmethod
    def probability(self, wires=None):
        """Return the (marginal) probability of each computational basis
        state from the last run of the device.

        If no wires are specified, then all the basis states representable by
        the device are considered and no marginalization takes place.

        Args:
            wires (Sequence[int]): Sequence of wires to return
                marginal probabilities for. Wires not provided
                are traced out of the system.

        Returns:
            array[float]: array of the probabilities
        """

    def marginal_prob(self, prob, wires=None):
        """Return the marginal probability of the computational basis
        states by summing the probabiliites on the non-specified wires.

        If no wires are specified, then all the basis states representable by
        the device are considered and no marginalization takes place.

        Args:
            prob: The probabilities to return the marginal probabilities
                for
            wires (Sequence[int]): Sequence of wires to return
                marginal probabilities for. Wires not provided
                are traced out of the system.

        Returns:
            list[float]: List of the resulting marginal probabilities.
        """
        wires = list(wires or range(self.num_wires))
        wires = np.hstack(wires)
        inactive_wires = list(set(range(self.num_wires)) - set(wires))
        prob = prob.reshape([2] * self.num_wires)
        return np.apply_over_axes(np.sum, prob, inactive_wires).flatten()

    def expval(self, observable):
        wires = observable.wires

        if self.analytic:
            # exact expectation value
            eigvals = observable.eigvals
            prob = self.probability(wires=wires)
            return (eigvals @ prob).real

        # estimate the ev
        return np.mean(self.sample(observable))

    def var(self, observable):
        wires = observable.wires

        if self.analytic:
            # exact variance value
            eigvals = observable.eigvals
            prob = self.probability(wires=wires)
            return (eigvals ** 2) @ prob - (eigvals @ prob).real ** 2

        # estimate the variance
        return np.var(self.sample(observable))

    def sample(self, observable):
        wires = observable.wires
        name = observable.name

        if isinstance(name, str) and name in {"PauliX", "PauliY", "PauliZ", "Hadamard"}:
            # Process samples for observables with eigenvalues {1, -1}
            return 1 - 2 * self._samples[:, wires[0]]

        # Replace the basis state in the computational basis with the correct eigenvalue.
        # Extract only the columns of the basis samples required based on ``wires``.
        wires = np.hstack(wires)
        samples = self._samples[:, np.array(wires)]
        unraveled_indices = [2] * len(wires)
        indices = np.ravel_multi_index(samples.T, unraveled_indices)
        return observable.eigvals[indices]
