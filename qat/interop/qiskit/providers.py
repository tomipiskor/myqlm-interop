
import os


from qiskit.providers import BaseBackend, BaseJob
from qiskit.providers.models.backendconfiguration import (
    BackendConfiguration,
    GateConfig,
)
from qiskit.result import Result
from qiskit.result.models import ExperimentResult, ExperimentResultData
from qiskit.assembler import disassemble
from qiskit.validation.base import Obj
from qiskit.providers.ibmq import least_busy
from qiskit import execute, Aer, IBMQ

# QLM imports
from qat.interop.qiskit.converters import to_qlm_circ
from qat.interop.qiskit.converters import job_to_qiskit_circuit
from qat.comm.datamodel.ttypes import QRegister
from qat.comm.shared.ttypes import Job, Batch
from qat.comm.shared.ttypes import Result as QlmRes
from qat.core.qpu.qpu import QPUHandler, get_registers
from qat.core.wrappers.result import State, aggregate_data
from qat.core.wrappers.result import Result as WResult
from qat.comm.shared.ttypes import Sample as ThriftSample
from collections import Counter
import numpy as np

def to_string(state, nbqbits):
    st = bin(state)[2:]
    st = "0" * (nbqbits - len(st)) + st
    return st


def generate_qlm_result(qiskit_result):
    """ Generates a QLM Result from a qiskit result

    Args:
        qiskit_result: The qiskit result to convert

    Returns:
        A QLM Result object built from the data in qiskit_result
    """

    nbshots = qiskit_result.results[0].shots
    try:
        counts = [vars(result.data.counts) for result in qiskit_result.results]
    except AttributeError:
        print("No measures, so the result is empty")
        return QlmRes(raw_data=[])
    counts = [{int(k, 16): v for k, v in count.items()} for count in counts]
    ret = QlmRes(raw_data=[])
    for state, freq in counts[0].items():
        if not isinstance(state, int):
            print("State is {}".format(type(state)))
        ret.raw_data.append(
            ThriftSample(state=state,
                         probability=freq / nbshots,
                         err=np.sqrt(freq / nbshots*(1.-freq/nbshots)/(nbshots-1))
                         if nbshots > 1 else None
                        )
        )
    return ret


def generate_experiment_result(qlm_result, nbqbits, head):
    """
    Generates an experiment result.
    Returns a qiskit.ExperimentResult structure.
    """
    counts = dict()
    samples = [hex(s.state.state) for s in qlm_result.raw_data]
    counts = dict(Counter(samples))
    data = ExperimentResultData.from_dict({"counts": counts})
    return ExperimentResult(
        shots=len(qlm_result.raw_data),
        success=True,
        data=data,
        header=Obj.from_dict(head),
    )


def generate_result(
    backend_name,
    backend_version,
    qobj_id,
    job_id,
    success,
    qlm_results,
    n_list,
    headers,
):
    """
    Tranform a QLM result into a qiskit result structure.
    Returns a qiskit.Result structure.
    """
    return Result(
        backend_name=backend_name,
        backend_version=backend_version,
        qobj_id=qobj_id,
        job_id=job_id,
        success=success,
        results=[
            generate_experiment_result(result, n, head)
            for result, n, head in zip(qlm_results, n_list, headers)
        ],
    )


class QLMJob(BaseJob):
    """
    QLM Job.
    QLM Job implement the required BaseJob interface of Qiskit with a small twist:
    everything is computed synchronously (meaning that the job is stored at submit and
    computed at result).
    """

    def add_results(self, qlm_result, qobj_id, n_list, headers):
        self._results = generate_result(
            self._backend._configuration.backend_name,
            self._backend._configuration.backend_version,
            qobj_id,
            self._job_id,
            True,
            qlm_result,
            n_list,
            headers,
        )

    def status(self):
        pass

    def cancel(self):
        pass

    def submit(self):
        pass

    def result(self):
        return self._results


## TODO :  can be improved by publishing any pyAQASM abstract gate via its circuit implementation.
## For now lets stick with the QASM usual gate set

_QLM_GATE_NAMES = [
    "id",
    "u0",
    "u1",
    "u2",
    "u3",
    "x",
    "y",
    "z",
    "h",
    "s",
    "sdg",
    "t",
    "tdg",
    "rx",
    "ry",
    "rz",
    "cx",
    "cy",
    "cz",
    "ch",
    "crz",
    "cu1",
    "cu3",
    "swap",
    "ccx",
    "cswap",
]

_QLM_GATES = [GateConfig(name="FOO", parameters=[], qasm_def="BAR")]

_QLM_PARAMS = {
    "backend_name": "QLMConnector",  # Name of the back end
    "backend_version": "0.0.1",  # Version of the back end
    "n_qubits": 100,  # Nb qbits
    "basis_gates": _QLM_GATE_NAMES,  # We accept all the gates of Qiskit (and more, but hey..)
    "gates": _QLM_GATES,  # They don't even use it for their simulators, so...
    "local": True,  # Its a local backend
    "simulator": True,  # Its a simulator. Is it though?
    "conditional": True,  # We support conditionals
    "open_pulse": False,  # We do not support open Pulse
    "memory": False,  # We do not support Memory (wth?)
    "max_shots": 4096,
}  # Max shots is 4096 (required :/)


class NoQpuAttached(Exception):
    pass


_QLM_BACKEND = BackendConfiguration(**_QLM_PARAMS)


class QLMBackend(BaseBackend):
    """
    Basic connector implementing a Qiskit Backend, plugable on a QLM QPUHandler.
    """

    def __init__(self, qpu=None, configuration=_QLM_BACKEND, provider=None):
        super(QLMBackend, self).__init__(configuration, provider)
        self.id_counter = 0
        self._qpu = qpu

    def set_qpu(self, qpu):
        """
            Sets the QPU that this backend is supposed to use
        """
        self._qpu = qpu

    def run(self, qobj):
        """ Convert all the circuits inside qobj into a Batch of
            QLM jobs before sending them into a QLM qpu

        Args:
            qobj: qiskit batch of circuits to run

        Returns:
            Returns the results of the QLM qpu execution after being
            converted into qiskit results
        """
        if self._qpu is None:
            raise NoQpuAttached("No qpu attached to the QLM connector.")
        headers = [exp.header.as_dict() for exp in qobj.experiments]
        circuits = disassemble(qobj)[0]
        nbshots = qobj.config.shots
        qlm_task = Batch(jobs=[])
        n_list = []
        for circuit in circuits:
            qlm_circuit = to_qlm_circ(circuit)
            job = qlm_circuit.to_job(aggregate_data=False)
            job.nbshots = nbshots
            job.qubits = [i for i in range(qlm_circuit.nbqbits)]
            n_list.append(job.qubits[-1]+1)
            qlm_task.jobs.append(job)

        results = self._qpu.submit(qlm_task)
        for res in results:
            for sample in res.raw_data:
                sample.intermediate_measures = None
            res = aggregate_data(res)

        # Creating a job that will contain the results
        job = QLMJob(self, str(self.id_counter))
        self.id_counter += 1
        job.add_results(results, qobj.qobj_id, n_list, headers)
        return job


class QiskitQPU(QPUHandler):
    """ 
        :class:`~qat.interop.qiskit.providers.QiskitQPU` is a 
        wrapper around any qiskit simulator/ quantum chip connection
        to follow how other standard QLM qpus work, this qpu is also
        synchronous, despite the asynchronous nature of qiskit's
        simulators. If you need an Asynchronous qiskit qpu, then use
        :class:`~qat.interop.qiskit.providers.AsyncQiskitQPU`
        this implementes :func:`~qat.interop.qiskit.providers.QiskitQPU.submit_job`


    Parameters:
        backend: The Backend qiskit object that is supposed to execute \
the circuit, if not supplied, QiskitQPU will try logging to IBMQ with \
the env variable QISKIT_TOKEN and QISKIT_URL, then select the least \
busy quantum chip available to you, if this fails, the backend will be \
qiskit's "qasm_simulator"
        plugins: Any plugins you want to add (c.f qat.core documentation)
        token: qiskit IBMQ login token, if not supplied loaded from env \
variable QISKIT_TOKEN
        url: qiskit IBMQ login url, if not supplied loaded from env \
variable QISKIT_URL, if not set, the hardcoded default: "https://api.quantum-computing.ibm.com/api/Hubs/ibm-q/Groups/open/Projects/main" is chosen

    """
    def __init__(self, backend=None, plugins=None, token=None, url=None):
        super(QPUHandler, self).__init__(plugins)
        self.set_backend(backend, token, url)

    def set_backend(self, backend=None, token=None, url=None):
        """ Sets the backend that will execute circuits

        Args:
            backend: The Backend qiskit object that is supposed to execute \
        the circuit, if not supplied, QiskitQPU will try logging to IBMQ with \
        the env variable QISKIT_TOKEN and QISKIT_URL, then select the least \
        busy quantum chip available to you, if this fails, the backend will be \
        qiskit's "qasm_simulator"
            plugins: Any plugins you want to add (c.f qat.core documentation)
            token: qiskit IBMQ login token, if not supplied loaded from env \
        variable QISKIT_TOKEN
            url: qiskit IBMQ login url, if not supplied loaded from env \
        variable QISKIT_URL, if not set, the hardcoded default: "https://api.quantum-computing.ibm.com/api/Hubs/ibm-q/Groups/open/Projects/main" is chosen

        Returns:
            None
        """
        if backend is None:
            if token is None:
                token = os.getenv("QISKIT_TOKEN")
            if token is not None:
                if url is None:
                        url = os.getenv("QISKIT_URL")
                if url is None:
                        url = "https://api.quantum-computing.ibm.com/api/Hubs/ibm-q/Groups/open/Projects/main"

                exists = False
                for entry in IBMQ.stored_accounts():
                    if entry['token'] == token:
                        exists = True
                        break
                if not exists:
                    IBMQ.save_account(token, url)
                IBMQ.load_accounts()
                #IBMQ.enable_account(token)
                self.backend = least_busy(IBMQ.backends(simulator=False))
            else:
                self.backend = Aer.get_backend("qasm_simulator")
        else:
            self.backend = backend

    def submit_job(self, qlm_job):
        if self.backend is None:
            raise ValueError("Backend cannot be None")

        qiskit_circuit = job_to_qiskit_circuit(qlm_job)
        qiskit_result = execute(qiskit_circuit, self.backend, shots=qlm_job.nbshots).result()
        res = generate_qlm_result(qiskit_result)
        return res

def wrap_result(job, res):
        """ Wrap a Result structure using the corresponding Job's information
        This is mainly to provide a cleaner/higher level interface for the user """
        qreg_list = None
        if job.circuit is not None:
            qreg_list = get_registers(job.circuit.qregs, job.qubits)
        if qreg_list is not None:
            res.wrap_samples(qreg_list)
        else:
            length = 0
            if job.qubits is not None:
                length = len(job.qubits)
            res.wrap_samples([QRegister(start=0, length=length, type=1)])

class Qiskitjob:
    """ Wrapper around qiskit's asynchronous calls"""
    def __init__(self, qlm_job, qobj):
        self._job_id = qobj.job_id()
        self._handler = qobj
        self._qlm_job = qlm_job

    def job_id(self):
        """ Returns the job's id"""
        return self._job_id

    def status(self):
        """ Returns the job status """
        return self._handler.status()._name_

    def result(self):
        """ Returns the result if available"""
        if self.status() == 'DONE':
            from qat.interop.qiskit.providers import generate_qlm_result
            result = generate_qlm_result(self._handler.result())
            result = WResult(result)
            wrap_result(self._qlm_job, result)
            return result

    def cancel(self):
        """ Attempts to cancel the job"""
        ret = self._handler.cancel()
        if ret:
            print("job successefully cancelled")
            return True
        else:
            print("Unable to cancel job")
            return False

class AsyncQiskitQPU(QPUHandler):
    """ Wrapper around any qiskit simulator/quantum chip connection.\
        Unlike the other wrapper, this one is asynchronous, and submitting\
        a job returns a qat.async.asyncqpu.Qiskitjob which is a wrapper\
        around any queries qiskit jobs offer, but with the exact same\
        interface as the QLM's Asyncjob

    Parameters:
        backend: The Backend qiskit object that is supposed to execute \
the circuit, if not supplied, QiskitQPU will try logging to IBMQ with \
the env variable QISKIT_TOKEN and QISKIT_URL, then select the least \
busy quantum chip available to you, if this fails, the backend will be \
qiskit's "qasm_simulator"
        plugins: Any plugins you want to add (c.f qat.core documentation)
        token: qiskit IBMQ login token, if not supplied loaded from env \
variable QISKIT_TOKEN
        url: qiskit IBMQ login url, if not supplied loaded from env \
variable QISKIT_URL, if not set, the hardcoded default: "https://api.quantum-computing.ibm.com/api/Hubs/ibm-q/Groups/open/Projects/main" is chosen

    """
    def __init__(self, backend=None, plugins=None, token=None, url=None):
        super(QPUHandler, self).__init__(plugins)
        self.set_backend(backend, token, url)

    def set_backend(self, backend=None, token=None, url=None):
        """ Sets the backend that will execute circuits

        Args:
            backend: The Backend qiskit object that is supposed to execute \
        the circuit, if not supplied, QiskitQPU will try logging to IBMQ with \
        the env variable QISKIT_TOKEN and QISKIT_URL, then select the least \
        busy quantum chip available to you, if this fails, the backend will be \
        qiskit's "qasm_simulator"
            plugins: Any plugins you want to add (c.f qat.core documentation)
            token: qiskit IBMQ login token, if not supplied loaded from env \
        variable QISKIT_TOKEN
            url: qiskit IBMQ login url, if not supplied loaded from env \
        variable QISKIT_URL, if not set, the hardcoded default: "https://api.quantum-computing.ibm.com/api/Hubs/ibm-q/Groups/open/Projects/main" is chosen

        Returns:
            None
        """
        if backend is None:
            if token is None:
                token = os.getenv("QISKIT_TOKEN")
            if token is not None:
                if url is None:
                    url = os.getenv("QISKIT_URL")
                if url is None:
                    url = "https://api.quantum-computing.ibm.com/api/Hubs/ibm-q/Groups/open/Projects/main"

                exists = False
                for entry in IBMQ.stored_accounts():
                    if entry['token'] == token:
                        exists = True
                        break

                if not exists:
                    IBMQ.save_account(token, url)
                IBMQ.load_accounts()
                #IBMQ.enable_account(token)
                self.backend = least_busy(IBMQ.backends(simulator=False))
            else:
                self.backend = Aer.get_backend("qasm_simulator")
        else:
            self.backend = backend

    def submit_job(self, qlm_job):
        """ Submits a QLM job to be executed on the previously\
 selected backend, if no backends are chosen an exception is raised

        Args:
            qlm_job: the qlm_job to be executed

        Returns:
            A Qiskitjob with the same interface as Asyncjob for the
            user to have information on their job execution
        """
        if self.backend is None:
            raise ValueError("Backend cannot be None")
        qiskit_circuit = job_to_qiskit_circuit(qlm_job)
        async_job = execute(qiskit_circuit, self.backend, shots=qlm_job.nbshots)
        return Qiskitjob(qlm_job, async_job)

    def submit(self, qlm_batch):
        """ Submits a QLM batch of jobs and returns the corresponding list
        of Qiskitjobs

        Args:
            batch: a batch of QLM jobs. If a single job is provided, the function calls the submit_job method, and returns a single Qiskitjob
        Returns:
            A list of Qiskitjob instances
        """
        if self.backend is None:
            raise ValueError("Backend cannot be None")
        if isinstance(qlm_batch, Job):
            return self.submit_job(qlm_batch)
        async_results = []
        for job in qlm_batch.jobs:
            async_results.append(self.submit_job(job))
        return async_results


class QLMConnector:
    def __or__(self, qpu):
        backend = QLMBackend(_QLM_BACKEND)
        backend.set_qpu(qpu)
        return backend


QLMConnector = QLMConnector()