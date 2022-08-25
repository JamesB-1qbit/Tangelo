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

"""Module to generate the circuits necessary to implement quantum signal processing
"""

import math
from typing import Union, Tuple, List
from copy import copy, deepcopy

import numpy as np
import pyqsp
from pyqsp import angle_sequence
from pyqsp.angle_sequence import AngleFindingError
from pyqsp.completion import CompletionError

from tangelo.linq import Gate, Circuit
from tangelo.toolboxes.operators import QubitOperator
from tangelo.linq.helpers.circuits.statevector import StateVector
from tangelo.toolboxes.circuits.lcu import get_uprep_uselect, sign_flip, get_lcu_qubit_op_info


def ham_sim_phases(tau: float, eps: float = 1.e-2, n_attempts: int = 10, method: str = "laurent") -> List[float]:

    pg = pyqsp.poly.PolyCosineTX()
    pcoefs, _ = pg.generate(tau=tau,
                            return_coef=True,
                            ensure_bounded=True,
                            return_scale=True, epsilon=eps)
    for i in range(n_attempts):
        try:
            phiset = angle_sequence.QuantumSignalProcessingPhases(
                pcoefs, eps=eps, suc=1-eps/10, method=method)
        except (AngleFindingError, CompletionError):
            if i == n_attempts-1:
                raise RuntimeError("Real phases calculation failed, increase n_attempts or decrease epsfac")
            else:
                print(f"Attempt {i+2} for the real coefficients")
        else:
            break
    pg = pyqsp.poly.PolySineTX()
    pcoefs, _ = pg.generate(tau=tau,
                            return_coef=True,
                            ensure_bounded=True,
                            return_scale=True, epsilon=eps)
    for i in range(n_attempts):
        try:
            phiset2 = angle_sequence.QuantumSignalProcessingPhases(
                pcoefs, eps=eps, suc=1-eps/10, method=method)
        except (AngleFindingError, CompletionError):
            if i == n_attempts-1:
                raise RuntimeError("Imaginary phases calculation failed, increase n_attempts or decrease epsfac")
            else:
                print(f"Attempt {i+2} for the imaginary phases")
        else:
            break
    return phiset + phiset2


def ham_sim_phases_QSPPACK(folder: str, tau: float, eps: float = 1.e-7) -> List[float]:
    from oct2py import octave
    from scipy.special import jn
    octave.addpath(folder)
    opts = octave.struct("criteria", eps)
    maxorder = math.ceil(1.4*tau+np.log(1e14))
    if maxorder % 2 == 1:
        maxorder -= 1

    coef = np.zeros((maxorder//2 + 1, 1), dtype=float, order='F')
    for i in range(1, len(coef)+1):
        coef[i-1][0] = (-1)**(i-1)*jn(2*(i-1), tau)

    coef[0] = coef[0]/2
    phi1, _ = octave.QSP_solver(coef, 0, opts, nout=2)

    coef = np.zeros((maxorder//2+1, 1), dtype=float, order='F')
    for i in range(1, len(coef)+1):
        coef[i-1][0] = (-1)**(i-1)*jn(2*i-1, tau)
    phi2, _ = octave.QSP_solver(coef, 1, opts, nout=2)
    return list(phi1.flatten()) + list(phi2.flatten())


def zero_controlled_cnot(qubit_list: List[int], target: Union[int, List[int]], control: Union[int, List[int]]) -> Circuit:
    x_ladder = [Gate("X", q) for q in qubit_list]
    gates = x_ladder + [Gate("CX", target=target, control=qubit_list+control)] + x_ladder
    return Circuit(gates)


def controlled_rotation(phi: float, target: Union[int, List[int]], control: Union[int, List[int]]) -> Circuit:
    gates = [Gate("CRZ", target=target, parameter=2*phi, control=control)]
    return Circuit(gates)


def f_p_fdag_no_anc(cua: Circuit, m_qs: List[int], angles: List[float], control: Union[int, List[int]] = None, with_z: bool = False) -> Circuit:
    c_list = list()
    if control is not None:
        c_list += control if isinstance(control, list) else [control]

    anc = m_qs[-1]+1
    if with_z:
        qubcirc = Circuit([Gate("CH", anc, control=c_list), Gate("CZ", anc, control=c_list)])
    else:
        qubcirc = Circuit([Gate("CH", anc, control=c_list)])

    pos_angles = []
    pos_angles = [angles[0]+np.pi/4]
    for ang in angles[1:-1]:
        pos_angles += [ang + np.pi/2]
    pos_angles += [angles[-1]+np.pi/4]

    zcnot = zero_controlled_cnot(m_qs, [anc], [])

    qubcirc += zcnot + controlled_rotation(pos_angles[-1], anc, c_list) + zcnot + cua
    for j, ang in enumerate(pos_angles[-2:0:-1]):
        qubcirc += Circuit([Gate("CZ", anc, control=c_list)]) + zcnot + controlled_rotation(ang, anc, c_list) + zcnot + cua
    qubcirc += Circuit([Gate("CZ", anc, control=c_list)]) + zcnot + controlled_rotation(pos_angles[0], anc, c_list) + zcnot

    qubcirc += Circuit([Gate("CH", anc, control=c_list)])
    return qubcirc


def get_qsp_circuit(qu_op: QubitOperator, tau: float, eps: float = 1.e-4, control: Union[int, List[int]] = None, n_attempts: int = 10,
                    method: str = 'laurent', folder: str = None) -> Circuit:
    """Returns Quantum Signal Processing (QSP) time-evolution circuit for a given QubitOperator for time tau"""

    if control is not None:
        control_list = copy(control) if isinstance(control, list) else [control]
    else:
        control_list = []

    swap_time = False
    if tau < 0.:
        qu_op = -qu_op
        tau = -tau
        swap_time = True
    q_qs, m_qs, alpha = get_lcu_qubit_op_info(qu_op)

    if method.lower() in ["laurent", "tf"]:
        angles = ham_sim_phases(tau*alpha, eps, n_attempts, method)
    elif method.lower() == "qsppack":
        angles = ham_sim_phases_QSPPACK(folder, tau*alpha, eps)

    # Leave gap of 1-qubit for f-circuit
    ext_qs = list(range(m_qs[-1]+2, m_qs[-1]+4))
    flip_qs = m_qs + [m_qs[-1]+1] + ext_qs

    # unpack real and imaginary angles.
    lang = len(angles)
    anglesr = angles[0:lang//2]
    anglesi = angles[lang//2:]

    uprep, uselect, _, _, _ = get_uprep_uselect(qu_op, control=ext_qs+control_list)
    cua = uprep + uselect + uprep.inverse()

    # Want 1-norm of coefficents to sum to 1/np.sin(np.pi/(2*(2*3+1))) so three oblivious amplitude amplifications results
    # in success probability of 1. |(cos(Ht)|=2+|iSin(Ht))|=2 so need to add (tarsum-2)/2 I - (tarsum-2)/2 I.
    tarsum = 1/np.sin(np.pi/(2*(2*3+1)))
    v = [(tarsum-4)/2, (tarsum-4)/2, 2, 2]
    v = np.sqrt(np.array(v))/np.sqrt(tarsum)

    s = StateVector(v, order="msq_first")
    uprep = s.initializing_circuit()
    uprep.reindex_qubits([ext_qs[0], ext_qs[1]])

    circ = uprep + Circuit([Gate("X", ext_qs[0])])
    # real part cos(Ht)
    circ += f_p_fdag_no_anc(cua, m_qs, anglesr, control=ext_qs+control_list, with_z=False)
    circ += Circuit([Gate("X", ext_qs[0])])
    # imaginary part i*sin(Ht)
    circ += f_p_fdag_no_anc(cua, m_qs, anglesi, control=ext_qs+control_list, with_z=False)
    # -I to ensure probability is np.arcsin
    circ += Circuit([Gate("X", ext_qs[1]), Gate("CRZ", target=0, parameter=2*np.pi, control=ext_qs+control_list), Gate("X", ext_qs[1])])

    circ += uprep.inverse()

    if control is not None:
        gates = [Gate("CRZ", q_qs[0], control=control, parameter=2*np.pi)] if len(anglesi) % 4 == 2 else []
    else:
        gates = [Gate("RZ", q_qs[0], parameter=2*np.pi)] if len(anglesi) % 4 == 2 else []
    if swap_time:
        qu_op = -qu_op
        tau = -tau

    return circ + (sign_flip(flip_qs) + circ.inverse() + sign_flip(flip_qs) + circ)*3 + Circuit(gates)