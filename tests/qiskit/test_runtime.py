# -*- coding: utf-8 -*-

"""
@authors    Arnaud Gazda <arnaud.gazda@atos.net>
@copyright  2022 Bull S.A.S. - All rights reserved
            This is not Free or Open Source software.
            Please contact Bull SAS for details about its license.
            Bull - Rue Jean Jaurès - B.P. 68 - 78340 Les Clayes-sous-Bois

Description: Testing the Qiskit Runtime QPU. This test file will mock the
             Estimator and Sampler services
"""

from dataclasses import dataclass

import unittest
import pytest
from qat.core import Batch, Observable
from qat.lang.AQASM import Program, H, CNOT

# The import will fail on 3.6, let's ignore it here, we use skipIf at the
# tests level to not run the tests
try:
    from qat.interop.qiskit.runtime import QiskitRuntimeQPU
except ImportError:
    pass

from hardware import running_python


@dataclass
class FakeQiskitResult:
    """
    Fake Qiskit result. A Qiskit result is composed of the following
    attributes:
     - quasi_dists: list of samples (in sampling mode)
     - values: list of energies (in observable mode)
     - metadata: list of metadata (a dictionnary containing the number of shots or
       the variance - depending on the primitive used)
    """
    # List of dictionnary - each dictionnary containing a pair "state" / "probability"
    quasi_dists: list = None

    # List of number - each value corresponding to the energy of the corresponding observable
    values: list = None

    # List of metadata
    metadata: list = None


class AbstractPrimitive:
    """
    Class describing an abstract primitive. A primitive is defined
    by a constructor and can by used as a context manager
    """
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        """
        Entering context
        """
        return self

    def __exit__(self, *args, **kwargs):
        """
        Exiting context
        """


class FakeSampler(AbstractPrimitive):
    """
    Class mocking the QiskitRuntime samples. This class will always return
    the same output
    """
    def __call__(self, circuit_indices: list, shots: int):
        """
        Execute a list of circuits
        """
        return FakeQiskitResult(
            quasi_dists=[{"00": 0.5, "11": 0.5} for _ in circuit_indices],
            metadata=[{"shots": shots} for _ in circuit_indices]
        )


class FakeEstimator(AbstractPrimitive):
    """
    Class mocking the QiskitRuntime estimator. This class will always return the
    same output
    """
    def __call__(self, circuit_indices, observable_indices, shots):
        """
        Execute a list of circuits
        """
        # Check arguments
        assert len(circuit_indices) == len(observable_indices)

        # Return result
        return FakeQiskitResult(
            values=[1. for _ in circuit_indices],
            metadata=[{"variance": 0} for _ in circuit_indices]
        )


# ########################################################### #
# Checking the sampling mode                                  #
# These tests submit Bell Pairs to a QPU, the expected output #
# is |00>: 0.5 - |11>: 0.5                                    #
# ########################################################### #


def _build_sample_job():
    """
    Build a sampling job
    The circuit is a Bell Pair
    """
    # Build program
    prog = Program()
    qbits = prog.qalloc(2)
    prog.apply(H, qbits[0])
    prog.apply(CNOT, qbits)

    # Build job
    circ = prog.to_circ()
    return circ.to_job()


def _check_one_result(result):
    """
    Checking a myQLM Result
    This test ensure the result correspond to |00>: 0.5 - |11>: 0.5
    """
    # Check number of samples
    assert len(result) == 2, "Invalid number of samples"

    # Check each samples
    for sample in result:
        assert sample.state.int in [0, 3], "Unexpected state"
        assert sample.probability == pytest.approx(0.5), "unexpected probability"


@pytest.mark.parametrize(
    ["jobs", "number_of_jobs"],
    [pytest.param(_build_sample_job(), 1, id="one job"),
     pytest.param([_build_sample_job(), _build_sample_job()], 2, id="list of jobs"),
     pytest.param(Batch(jobs=[_build_sample_job(), _build_sample_job()]), 2, id="one batch")]
)
@unittest.skipIf(running_python("3.6"), "Test not supported")
def test_sampling_mode(mocker, jobs, number_of_jobs):
    """
    Testing IBM QPU in sampling mode
    This test submit a Bell Pair and checks the result
    """
    # Mock sampler
    mocker.patch("qat.interop.qiskit.runtime.Sampler", FakeSampler)

    # Submit job
    qpu = QiskitRuntimeQPU(backend="NO BACKEND", service="NO SERVICE")
    results = qpu.submit(jobs)

    # Check result
    if number_of_jobs == 1:
        _check_one_result(results)
        return

    assert len(results) == number_of_jobs

    for result in results:
        _check_one_result(result)


# ########################################################### #
# Checking the observable mode                                #
# These tests submit a circuit composed of a single H gate,   #
# measure "X" observable and check the average value          #
# ########################################################### #

def _build_observable_job():
    """
    Build an observable job. The circuit is composed of a single
    gate "H" and the measure observable is "X"
    """
    # Build program
    prog = Program()
    qbit = prog.qalloc(1)
    prog.apply(H, qbit)

    # Build job
    circ = prog.to_circ()
    return circ.to_job("OBS", observable=Observable.sigma_x(0, 1))


@pytest.mark.parametrize(
    ["jobs", "number_of_jobs"],
    [pytest.param(_build_observable_job(), 1, id="one job"),
     pytest.param([_build_observable_job(), _build_observable_job()], 2, id="list of jobs"),
     pytest.param(Batch(jobs=[_build_observable_job(), _build_observable_job()]), 2, id="one batch")]
)
@unittest.skipIf(running_python("3.6"), "Test not supported")
def test_observable_mode(mocker, jobs, number_of_jobs):
    """
    Testing IBM QPU in observable mode
    This test submit a circuit composed of an H gate and measure the "X"
    observable
    """
    # Mock sampler
    mocker.patch("qat.interop.qiskit.runtime.Estimator", FakeEstimator)

    # Submit job
    qpu = QiskitRuntimeQPU(backend="NO BACKEND", service="NO SERVICE")
    results = qpu.submit(jobs)

    if number_of_jobs == 1:
        results = [results]

    # Check result
    assert len(results) == number_of_jobs

    for result in results:
        assert result.value == pytest.approx(1.)
        assert result.error == pytest.approx(0.)
