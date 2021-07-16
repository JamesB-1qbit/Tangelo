import unittest

from pyscf import gto

from qsdk.problem_decomposition.dmet.dmet_problem_decomposition import Localization, DMETProblemDecomposition

H4_RING = [['H', [0.7071067811865476,   0.0,                 0.0]],
           ['H', [0.0,                  0.7071067811865476,  0.0]],
           ['H', [-1.0071067811865476,  0.0,                 0.0]],
           ['H', [0.0,                 -1.0071067811865476,  0.0]]]


class DMETVQETest(unittest.TestCase):

    def test_h4ring_vqe_uccsd(self):
        """ DMET on H4 ring with fragment size one, using VQE-UCCSD """

        mol = gto.Mole()
        mol.atom = H4_RING
        mol.basis = "minao"
        mol.charge = 0
        mol.spin = 0
        mol.build()

        opt_dmet = {"molecule": mol,
                    "fragment_atoms": [1, 1, 1, 1],
                    "fragment_solvers": ['vqe', 'ccsd', 'ccsd', 'ccsd'],
                    "electron_localization": Localization.meta_lowdin,
                    "verbose": False
                    }

        # Run DMET
        dmet = DMETProblemDecomposition(opt_dmet)
        dmet.build()
        energy = dmet.simulate()

        self.assertAlmostEqual(energy, -1.9916120594, delta=1e-3)

    def test_h4ring_vqe_ressources(self):
        """Resources estimation on H4 ring. """

        mol = gto.Mole()
        mol.atom = H4_RING
        mol.basis = "minao"
        mol.charge = 0
        mol.spin = 0
        mol.build()

        opt_dmet = {"molecule": mol,
                    "fragment_atoms": [1, 1, 1, 1],
                    "fragment_solvers": ["vqe", "ccsd", "ccsd", "ccsd"],
                    "electron_localization": Localization.meta_lowdin,
                    "verbose": False
                    }

        # Building DMET fragments (with JW).
        dmet = DMETProblemDecomposition(opt_dmet)
        dmet.build()
        resources_jw = dmet.get_resources()

        # Building DMET fragments (with scBK).
        opt_dmet["solvers_options"] = {"qubit_mapping": "scbk", "up_then_down": True}
        dmet = DMETProblemDecomposition(opt_dmet)
        dmet.build()
        resources_bk = dmet.get_resources()

        # JW.
        self.assertEqual(resources_jw[0]["qubit_hamiltonian_terms"], 15)
        self.assertEqual(resources_jw[0]["circuit_width"], 4)
        # scBK.
        self.assertEqual(resources_bk[0]["qubit_hamiltonian_terms"], 5)
        self.assertEqual(resources_bk[0]["circuit_width"], 2)


if __name__ == "__main__":
    unittest.main()