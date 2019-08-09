#!/usr/bin/env python
# -*- coding: utf-8 -*-
#@brief
#
#@file qat/interop/pyquil/providers.py
#@namespace qat.interop.pyquil.providers
#@authors Reda Drissi <mohamed-reda.drissi@atos.net>
#@copyright 2019  Bull S.A.S.  -  All rights reserved.
#           This is not Free or Open Source software.
#           Please contact Bull SAS for details about its license.
#           Bull - Rue Jean Jaurès - B.P. 68 - 78340 Les Clayes-sous-Bois

"""
Providers functions and classes for pyquil

"""
from pyquil import get_qc

from qat.interop.pyquil.converters import to_pyquil_circ
from qat.core.qpu.qpu import QPUHandler
from qat.core.wrappers.result import State
from qat.comm.shared.ttypes import Result as QlmRes
from qat.comm.shared.ttypes import Sample as ThriftSample
from qat.comm.shared.ttypes import Job

from collections import Counter
import numpy as np

def generate_qlm_result(pyquil_result):
    """ Converts pyquil result to QLM Result

    Args:
        pyquil_result: The result object generated by pyquil
    Returns:
        A QLM Result object built from pyquil_result
    """

    # Pyquil encodes measures in a matrix, where line i is the measures
    # for trial i, and column j contains the measurements for qubit j

    # Build a list of states

    nbshots = len(pyquil_result)
    measurements = [
        sum([b << i for i, b in enumerate(entry)]) for entry in pyquil_result
    ]

    counts = Counter(measurements)
    qlm_result = QlmRes()
    qlm_result.raw_data = [
        ThriftSample(state=state,
                     probability=freq / nbshots,
                     err=np.sqrt(freq / nbshots*(1.-freq/nbshots)(nbshots-1))
                     if nbshots > 1 else None
                    )
        for state, freq in counts.items()
    ]
    return qlm_result


class PyquilQPU(QPUHandler):
    """
        QPU wrapper over pyquil, to run a QLM circuit on a pyquil
        simulator or rigetti's quantum chip

    Args:
        qpu: the instance of pyquil's simulator/connection to real
               quantum chip or simulator
        plugins: plugins to use
        compiler: if set to True(default value) the circuit will be
                    compiled by pyquil, otherwise the user compiles
                    the circuit manually and tells the pyquil qpu to
                    skip compilation
    """
    def __init__(self, qpu=None, plugins=None, compiler=True):
        super(QPUHandler, self).__init__(plugins)
        self.qpu = qpu
        self.compiler = True

    def set_qpu(self, qpu):
        self.qpu = qpu

    def submit_job(self, qlm_job):
        qlm_circuit = qlm_job.circuit
        pyquil_circuit = to_pyquil_circ(qlm_circuit)
        if self.compiler:
            try:
                executable = self.qpu.compile(pyquil_circuit)
            except AttributeError:
                executable = pyquil_circuit
        else:
            executable = pyquil_circuit
        # qc.run_and_measure(pyquil_circuit, trials=1)
        result = generate_qlm_result(self.qpu.run(executable))
        return result

    def __submit(self, qlm_batch):
        if isinstance(qlm_batch, Job):
            return self.submit_job(qlm_batch)
        else:
            results = []

            for job in qlm_batch.jobs:
                results.append(self.submit_job(job))
            return results

