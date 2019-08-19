# ******************
# MODULE DOCSTRING
# ******************

"""

LOMAP: Maximum Common Subgraph and scoring calculations
=====

Alchemical free energy calculations hold increasing promise as an aid to drug 
discovery efforts. However, applications of these techniques in discovery 
projects have been relatively few, partly because of the difficulty of planning 
and setting up calculations. The Lead Optimization Mapper (LOMAP) is an 
automated algorithm to plan efficient relative free energy calculations between 
potential ligands within a substantial of compounds.

"""

# *****************************************************************************
# Lomap2: A toolkit to plan alchemical relative binding affinity calculations
# Copyright 2015 - 2016  UC Irvine and the Authors
#
# Authors: Dr Gaetano Calabro' and Dr David Mobley
# 
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, see http://www.gnu.org/licenses/
# *****************************************************************************


# ****************
# MODULE IMPORTS
# ****************


from rdkit import Chem
from rdkit.Chem import rdFMCS
from rdkit.Chem import AllChem
from rdkit.Chem.Draw.MolDrawing import DrawingOptions
from rdkit.Chem import Draw
from rdkit.Chem import rdmolops
from rdkit import DataStructs
from rdkit.Chem.Fingerprints import FingerprintMols
from rdkit.Geometry.rdGeometry import Point3D
import sys
import math
from rdkit import RDLogger
import logging
import argparse

# *******************************
# Maximum Common Subgraph Class
# *******************************


__all__ = ['MCS']


class MCS(object):
    """

    This class is used to compute the Maximum Common Subgraph (MCS) between two
    RDkit molecule objects and to score their similarity by using defined rules 
    
    """

    def __init__(self, moli, molj, options=argparse.Namespace(time=20, verbose='info', max3d=1000, threed=False)):
        """
        Initialization function
    
        Parameters
        ----------

        moli : RDKit molecule object 
            the first molecule used to perform the MCS calculation
        molj : RDKit molecule object 
            the second molecule used to perform the MCS calculation
        options : argparse python object 
            the list of user options 
       
        """

        def substructure_centre(mol, mol_sub):
            """

            This function takes a molecule and a list of atom indices
            in that molecule and returns an RDKit Point3D representing
            the geometric centre of the atoms in the list

            """

            sum = Point3D()
            for i in mol_sub:
                sum += mol.GetConformer().GetAtomPosition(i)
            return sum / len(mol_sub)


        def best_substruct_match_to_mcs(moli,molj,by_rmsd=True):
            """

            This function looks over all of the substructure matches and returns the one
            with the best 3D correspondence (if by_rmsd is true), or the fewest number
            of atomic number mismatches (if by_rmsd is false)

            Note that the 3D correspondence does a translational centreing (but
            does not rotate).

            """

            # Sanity checking
            if not moli.HasSubstructMatch(self.mcs_mol):
                raise ValueError('RDkit MCS Subgraph first molecule search failed')

            if not molj.HasSubstructMatch(self.mcs_mol):
                raise ValueError('RDkit MCS Subgraph second molecule search failed')

            moli_sub = moli.GetSubstructMatches(self.mcs_mol,uniquify=False)
            molj_sub = molj.GetSubstructMatches(self.mcs_mol,uniquify=False)
            best_rmsd=1e8
            for mapi in moli_sub:
                for mapj in molj_sub:
                    # Compute the translation to bring molj's centre over moli
                    coord_delta = (substructure_centre(moli,mapi)
                                 - substructure_centre(molj,mapj))
                    rmsd=0
                    for pair in zip(mapi,mapj):
                        if by_rmsd:
                            rmsd += (moli.GetConformer().GetAtomPosition(pair[0]) 
                                   - molj.GetConformer().GetAtomPosition(pair[1])
                                   - coord_delta).LengthSq()
                        elif (moli.GetAtomWithIdx(pair[0]).GetAtomicNum() != 
                              molj.GetAtomWithIdx(pair[1]).GetAtomicNum()):
                            rmsd+=1
                    if rmsd < best_rmsd:
                        besti=mapi
                        bestj=mapj
                        best_rmsd=rmsd

            return (besti,bestj)

        def trim_mcs_mol(max_deviation=2.0):
            """

            This function is used to trim the MCS molecule to remove mismatched atoms i.e atoms
            where the topological mapping does not work in 3D coordinates.

            The sets of mapped atoms are translated to bring their geometric centres
            into alignment before trimming
           
            Parameters
            ----------

            max_deviation : the maximum difference in Angstroms between mapped atoms to allow

            """

            while True:
                (mapi,mapj) = best_substruct_match_to_mcs(self.__moli_noh,self.__molj_noh,by_rmsd=True)
                # Compute the translation to bring molj's centre over moli
                coord_delta = (substructure_centre(self.__moli_noh,mapi)
                             - substructure_centre(self.__molj_noh,mapj))
                worstatomidx=-1
                worstdist=0
                atomidx=0
                for pair in zip(mapi,mapj):
                    dist = (self.__moli_noh.GetConformer().GetAtomPosition(pair[0])
                          - self.__molj_noh.GetConformer().GetAtomPosition(pair[1])
                          - coord_delta).Length()
                    if dist > worstdist:
                        worstdist=dist
                        worstatomidx=atomidx
                    atomidx=atomidx+1

                if worstdist > max_deviation:
                    # Remove the furthest-away atom and try again
                    rwm = Chem.RWMol(self.mcs_mol)
                    print("REMOVING ATOM",worstatomidx," with distance", worstdist)
                    rwm.RemoveAtom(worstatomidx)
                    self.mcs_mol=Chem.Mol(rwm)
                else:
                    break


        def trim_mcs_chiral_atoms():
            """
                Remove all atoms in the MCS where there might be a chirality inversion i.e.
                (a) the corresponding atoms in the input molecules are both chiral, and
                (b) the parity of the atom mapping in the input molecules is reversed

                Calls map_mcs_mol as it uses the mappings generated there. 

            """

            def reorder_mol_to_mcs(mol):
                """ 
                    Reorder a molecule so that its atoms are in the same order as the MCS,
                    using the 'to_mcs' property. Return the reordered molecule (as a copy).
                """
                newindexes = list(range(mol.GetNumAtoms()))

                # Find the atom mapping to atom 0 in the MCS, and swap
                # it with position 0. Rinse and repeat
                for i in range(mol.GetNumAtoms()):
                    for j in range(i+1,mol.GetNumAtoms()):
                        if (mol.GetAtomWithIdx(j).HasProp('to_mcs')):
                            if int(mol.GetAtomWithIdx(j).GetProp('to_mcs'))==i :
                                newindexes[i],newindexes[j] = newindexes[j],newindexes[i]

                reordered_mol_copy = Chem.RenumberAtoms(mol,newindexes)
                return reordered_mol_copy

            def flag_inverted_atoms_in_mcs():
                """
                    Flag all atoms in the MCS where the chirality is inverted between
                    moli and molj with CHI_TETRAHEDRAL_CW)
                """
                # Generate atommappings as they are useful below
                map_mcs_mol()

                # moli chiral atoms
                rmoli = reorder_mol_to_mcs(self.moli)
                chiral_at_moli = [seq[0] for seq in Chem.FindMolChiralCenters(rmoli)]

                # molj chiral atoms
                rmolj = reorder_mol_to_mcs(self.molj)
                chiral_at_molj = [seq[0] for seq in Chem.FindMolChiralCenters(rmolj)]

                invertedatoms = []

                for i in chiral_at_moli:
                    # Is atom i in the MCS?
                    ai = rmoli.GetAtomWithIdx(i)
                    if (ai.HasProp('to_mcs')):
                        #print("Checking mol i chiral atom",i,ai.GetProp('to_mcs'))
                        for j in chiral_at_molj:
                            # Is atom j in the MCS?
                            aj = rmolj.GetAtomWithIdx(j)
                            if (aj.HasProp('to_mcs')):
                                #print("Matching mol j chiral atom",j,aj.GetProp('to_mcs'))
                                # Are they the same atom?
                                if (ai.GetProp('to_mcs') == aj.GetProp('to_mcs')):
                                    #print("Matched mcs atom ",aj.GetProp('to_mcs'),"inverted?",ai.GetChiralTag()!=aj.GetChiralTag())

                                    # OK, atoms are both chiral, and match the same MCS atom
                                    # Check if the parities are the same. If not, flag with the
                                    # CHI_TETRAHEDRAL_CW property
                                    if (ai.GetChiralTag()!=aj.GetChiralTag()):
                                        invertedatoms.append(int(aj.GetProp('to_mcs')))

                for i in invertedatoms:
                    mcsat = self.mcs_mol.GetAtomWithIdx(i)
                    mcsat.SetChiralTag(Chem.rdchem.ChiralType.CHI_TETRAHEDRAL_CW)
                    if options.verbose == 'pedantic':
                       logging.info('Chiral atom detected: %d' %(i))

            #print("MCS before chiral trimming: ",Chem.MolToSmiles(self.mcs_mol))

            # Flag inverted atoms
            flag_inverted_atoms_in_mcs()

            # Trim inverted chiral Atoms. The algorithm is to delete the chiral centre,
            # fragment the molecule, and keep only the two largest fragments. Rinse and
            # repeat until no more flagged chiral centres remain
            # 
            # Keep

            while True:
                mcs_chiral_set = set()
                atom_idx = -1;

                for atom in self.mcs_mol.GetAtoms():
                    # Note that any atom in the MCS which is chiral in either input mol is
                    # flagged with CHI_TETRAHEDRAL_CW
                    if (atom.GetChiralTag() == Chem.rdchem.ChiralType.CHI_TETRAHEDRAL_CW):
                        atom_idx=atom.GetIdx()
                        atom.SetChiralTag(Chem.rdchem.ChiralType.CHI_UNSPECIFIED)
                        break

                if atom_idx == -1:  # Not found any more chiral atoms, so done
                    break

                # Move the chiral atom to the end (avoids indexing problems)
                newindexes = list(range(self.mcs_mol.GetNumAtoms()))
                newindexes.remove(atom_idx)
                newindexes.append(atom_idx)
                self.mcs_mol = Chem.RenumberAtoms(self.mcs_mol,newindexes)

                # Now we loop, deleting groups attached to the chiral atom, until the 
                # chiral atom has at most two heavy atom connections
                # Note that getAtoms()[-1] returns the first atom not the last if you 
                # don't convert it to a list. Grr.
                while list(self.mcs_mol.GetAtoms())[-1].GetDegree() > 2 :

                    #print("MCS mol is",Chem.MolToSmiles(self.mcs_mol),self.mcs_mol.GetNumHeavyAtoms())

                    # Delete the chiral atom in a temporary molecule, and fragment. Since the
                    # chiral atom was the last one, the indexes in the temporary molecule are the
                    # same as in self.mcs_mol
                    edit_mol = Chem.EditableMol(self.mcs_mol)
                    edit_mol.RemoveAtom(self.mcs_mol.GetNumAtoms()-1)
                    tmp_mol = edit_mol.GetMol()
                    fragments = Chem.rdmolops.GetMolFrags(tmp_mol)
                    #print("Fragments are" ,fragments)

                    # Get index of smallest fragments
                    min_idx = 0
                    lgt_min = 10000

                    for idx in range(0, len(fragments)):
                        lgt = len(fragments[idx])
                        if lgt < lgt_min:
                            lgt_min = lgt
                            min_idx = idx

                    # Get the atoms in this fragment and sort them so we delete the
                    # largest index first
                    min_frag = list(fragments[min_idx])
                    min_frag.sort(reverse=True)

                    edit_mol = Chem.EditableMol(self.mcs_mol)
                    for idx in min_frag:
                        edit_mol.RemoveAtom(idx)
                    self.mcs_mol = edit_mol.GetMol()

            # Done!
            #print("Reduced MCS after chiral trimming: ",Chem.MolToSmiles(self.mcs_mol))

        def map_mcs_mol():
            """

            This function is used to define a map between the generated mcs, the
            molecules and vice versa
           
            """

            # Get self-mapping for the MCS
            mcsi_sub = tuple(range(self.mcs_mol.GetNumAtoms()))

            (moli_sub,molj_sub) = best_substruct_match_to_mcs(self.__moli_noh,self.__molj_noh,by_rmsd=self.options.threed)

            # mcs to moli
            map_mcs_mol_to_moli_sub = list(zip(mcsi_sub, moli_sub))

            # Clear all properties as we may call this function more than once
            for a in self.mcs_mol.GetAtoms():
                a.ClearProp('to_moli')
                a.ClearProp('to_molj')
            for a in self.moli.GetAtoms():
                a.ClearProp('to_mcs')
            for a in self.molj.GetAtoms():
                a.ClearProp('to_mcs')

            # An RDkit atomic property is defined to store the mapping to moli
            for idx in map_mcs_mol_to_moli_sub:
                self.mcs_mol.GetAtomWithIdx(idx[0]).SetProp('to_moli', str(idx[1]))
                self.moli.GetAtomWithIdx(idx[1]).SetProp('to_mcs', str(idx[0]))

            mcsj_sub = tuple(range(self.mcs_mol.GetNumAtoms()))

            # mcs to molj
            map_mcs_mol_to_molj_sub = list(zip(mcsj_sub, molj_sub))

            # Map between the two molecules
            self.__map_moli_molj = list(zip(moli_sub, molj_sub))

            # An RDkit atomic property is defined to store the mapping to molj
            for idx in map_mcs_mol_to_molj_sub:
                self.mcs_mol.GetAtomWithIdx(idx[0]).SetProp('to_molj', str(idx[1]))
                self.molj.GetAtomWithIdx(idx[1]).SetProp('to_mcs', str(idx[0]))

            # For each mcs atom we save its original index in a specified 
            # property. This could be very useful in the code development
            # when deletion or atom insertions are performed
            for at in self.mcs_mol.GetAtoms():
                at.SetProp('org_idx', str(at.GetIdx()))

            return

        def set_ring_counter(mol):

            """

            This function is used to attach to each molecule atom a ring counter
            rc. This parameter is used to asses if a ring has been broken or not
            during the MCS mapping
         
            Parameters
            ----------
            mol : RDKit Molecule obj
                the molecule used to define the atom ring counters
            """

            # set to zero the atom ring counters
            for at in mol.GetAtoms():
                at.SetProp('rc', '0')

            rginfo = mol.GetRingInfo()

            rgs = rginfo.AtomRings()

            # print rgs

            rgs_set = set([e for l in rgs for e in l])

            for idx in rgs_set:
                for r in rgs:
                    if idx in r:
                        val = int(mol.GetAtomWithIdx(idx).GetProp('rc'))
                        val = val + 1
                        mol.GetAtomWithIdx(idx).SetProp('rc', str(val))
            return

        def sanity_check_on_molecule(mol):
            # Sanity check: we require all heavies to be before all hydrogens in the input
            lasta=mol.GetAtomWithIdx(0)
            for a in mol.GetAtoms():
                if (a.GetAtomicNum()>1 and lasta.GetAtomicNum()==1):
                    raise ValueError('Hydrogens not after all heavy atoms in molecule')
                lasta=a

        # START of __init__ function
        # Set logging level and format
        logging.basicConfig(format='%(levelname)s:\t%(message)s', level=logging.INFO)

        self.options=options

        # Local pointers to the passed molecules
        self.moli = moli
        sanity_check_on_molecule(self.moli)
        self.molj = molj
        sanity_check_on_molecule(self.molj)

        # Sanitize input molecules
        Chem.SanitizeMol(self.moli)
        Chem.SanitizeMol(self.molj)

        # Set chirality flags from 3D coords if working in 3D
        if self.options.threed:
            Chem.rdmolops.AssignAtomChiralTagsFromStructure(self.moli,replaceExistingTags=True)
            Chem.rdmolops.AssignAtomChiralTagsFromStructure(self.molj,replaceExistingTags=True)

        if not options.verbose == 'pedantic':
            lg = RDLogger.logger()
            lg.setLevel(RDLogger.CRITICAL)

        # Local pointers to the passed molecules without hydrogens
        # These variables are defined as private
        try:
            self.__moli_noh = AllChem.RemoveHs(moli)
            self.__molj_noh = AllChem.RemoveHs(molj)
        except Exception:
            self.__moli_noh = AllChem.RemoveHs(moli, sanitize=False)
            self.__molj_noh = AllChem.RemoveHs(molj, sanitize=False)

            Chem.SanitizeMol(self.__moli_noh, sanitizeOps=Chem.SanitizeFlags.SANITIZE_SETAROMATICITY)
            Chem.SanitizeMol(self.__molj_noh, sanitizeOps=Chem.SanitizeFlags.SANITIZE_SETAROMATICITY)

        # MCS calculation. In RDKit the MCS is a smart string. Ring atoms are 
        # always mapped in ring atoms. 
        # Don't add the mcs result as a member variable as it can't be pickled
        __mcs = rdFMCS.FindMCS([self.__moli_noh, self.__molj_noh],
                                    timeout=options.time,
                                    atomCompare=rdFMCS.AtomCompare.CompareAny,
                                    bondCompare=rdFMCS.BondCompare.CompareAny,
                                    matchValences=False,
                                    ringMatchesRingOnly=True,
                                    completeRingsOnly=True,
                                    matchChiralTag=False)

        # Note that we need matchChiralTag=False as we want to match chiral atoms with different
        # parities, we just need to trim the MCS to the largest possible match that doesn't have
        # a mismatched chiral centre in it (eg we want to match FC[C@](C)CO to FC[C@@](C)CO
        # using the MCS FCCCO - this includes the chiral atom but we delete the methyl group
        # that led it to be chiral. The trimming is done in trim_mcs_chiral_atoms()

        # Checking
        if __mcs.canceled:
            logging.warning('Timeout reached to find the MCS between the molecules')

        if __mcs.numAtoms == 0:
            raise ValueError('No MCS was found between the molecules')

        # The found MCS pattern (smart strings) is converted to a RDKit molecule
        self.mcs_mol_smarts = __mcs.smartsString
        self.mcs_mol = Chem.MolFromSmarts(__mcs.smartsString)

        # There's a symmetry-related bug here: if there was more than one MCS
        # of the same size and score, we'll get only one at random. We then try
        # to choose the mapping that matches 3D coords the best, but one of the 
        # not-considered MCSes that we never saw may give a better mapping. 
        # We can rescue some of this by converting all partial-query atoms to 
        # full query atoms
        testmol = Chem.MolFromSmarts("*")   # Create a "match anything" query atom for us to copy
        for a in self.mcs_mol.GetAtoms():
            if a.DescribeQuery().startswith("AtomOr"):  # Matches more than one element
                a.SetQuery(testmol.GetAtoms()[0])   # Set this atom to a copy of the "match anything" atom

        try:  # Try to sanitize the MCS molecule
            Chem.SanitizeMol(self.mcs_mol)
        except Exception:  # if not, try to recover the atom aromaticity which is
            # important for the ring counter
            sanitFail = Chem.SanitizeMol(self.mcs_mol, sanitizeOps=Chem.SanitizeFlags.SANITIZE_SETAROMATICITY,
                                         catchErrors=True)
            if sanitFail:  # if not, the MCS is skipped
                raise ValueError('Sanitization Failed...')

        # Trim the MCS to remove atoms with too-large real-space deviations
        if self.options.max3d>0 :
            try:
                trim_mcs_mol(max_deviation=self.options.max3d)
            except Exception as e:
                raise ValueError(str(e))

        # Trim the MCS further to remove chirality mismatches
        trim_mcs_chiral_atoms()

        # Mapping between the found MCS molecule and moli,  molj
        try:
            map_mcs_mol()
        except Exception as e:
            raise ValueError(str(e))

        # Set the ring counters for each molecule
        set_ring_counter(self.__moli_noh)
        set_ring_counter(self.__molj_noh)
        set_ring_counter(self.mcs_mol)

        # for at in self.mcs_mol.GetAtoms():
        #     print 'at = %d rc = %d' % (at.GetIdx(), int(at.GetProp('rc')))

        if not options.verbose == 'pedantic':
            lg.setLevel(RDLogger.WARNING)

        return

    def get_map(self):
        """

        This function is used to return a list of pairs of atom indexes generated
        by the mapping between the two molecules used to calculate the MCS. 
        The calculated mapping is performed without considering hydrogens 

        Returns
        -------
        pair of indexes related to the atom mapping 

        """

        return self.__map_moli_molj

    ############ MCS BASED RULES ############

    # MCSR Rule
    # the mtansr method is not used but be retained here in case need to use in the future.
    def mtansr(self, ):
        """
        This rule computes the structural similarity between the two passed molecules 
        using the tanimoto score. 
        Returns
        -------
        scr_tan : float
            the rule score
        """
        fps_moli = FingerprintMols.FingerprintMol(self.moli)
        fps_molj = FingerprintMols.FingerprintMol(self.molj)
        scr_tan = DataStructs.FingerprintSimilarity(fps_moli, fps_molj)
        return scr_tan

    def mcsr(self, beta=0.1):

        """
        This rule computes the similarity between the two passed molecules 
        used to compute the MCS
        
        Parameters
        ----------
        beta : float
            a parameter used to refine the exponential function used in the
            scoring

        Returns
        -------
        scr_mcsr : float
            the rule score

             
        """

        # The number of heavy atoms in each molecule
        nha_moli = self.moli.GetNumHeavyAtoms()
        nha_molj = self.molj.GetNumHeavyAtoms()
        # Note that the mcs_mol (a) doesn't contain hydrogens, and (b) does contain
        # wildcard atoms, which don't count as 'heavy'. Use the total atom count instead.
        nha_mcs_mol = self.mcs_mol.GetNumAtoms()

        # score
        scr_mcsr = math.exp(-beta * (nha_moli + nha_molj - 2 * nha_mcs_mol))
        print("MCSR from",nha_moli,nha_molj,' common',nha_mcs_mol,"is",scr_mcsr)

        return scr_mcsr

    # MNACR rule
    def mncar(self, ths=4):

        """
        This rule cut the similarity score between two molecules if they do
        not share the selected number of atoms 

        
        Parameters
        ----------
        ths : float
            the minumum number of atoms to share
        
        Returns
        -------
        scr_mncar : float
            the rule score     
        """

        # This rule has been modified from the rule desribed in the Lomap paper
        # to match the LOMAP first implementation provided by schrodinger

        nha_mcs_mol = self.mcs_mol.GetNumHeavyAtoms()
        nha_moli = self.moli.GetNumHeavyAtoms()
        nha_molj = self.molj.GetNumHeavyAtoms()

        scr_mncar = float((nha_mcs_mol >= ths) or (nha_moli < ths + 3) or (nha_molj < ths + 3))

        return scr_mncar

    # TMCRS rule (Trim rule) 
    # MDM Note: we don't use this as we don't have the same limitation on partial ring
    # deletion as Schrodinger
    # NB removed the chirality check - the MCS is now trimmed to remive chirality
    def tmcsr(self, beta=0.1, strict_flag=True):

        """
        This rule check if rings have been broken during the MCS mapping 
        and if chiral atoms are presents. If rings are broken all the 
        remaining ring atoms are deleted. Atoms connected to chiral centers
        are deleted as well

        Note that if no rings or broken of chiral centres need to be removed this
        will return 1.0

 
        Parameters
        ----------
        beta : float
            a parameter used to refine the exponential function used 
            in the scoring
            
        stric_flag : bool
            a flag used to select the scrict or loose mode
             
        """

        def delete_broken_ring():

            # Strict: we cancel all the atoms in conflict in the mcs and 
            # delete all eventually non ring atoms that are left 
            def extend_conflict(mol, conflict):
                """
            
                This function check if rings have been broken during the MCS mapping
                deleting all the remaining atom rings. In strict mode all the 
                conflicting ring atoms are deleted. In loose mode only non planar
                atom rings are deleted

                Parameters
                ----------
                mol : RDKit molecule obj
                    the mcs molecule
                conflict : set
                    the set of atoms in Moli and Molj that are in conflict with 
                    the MCS molecule. A conflict is generated if the ring counter
                    between the MCS and Moli/Molj changes

                Returns
                -------
                mcs_mol : RDKit molecule obj
                    a copy of the edited mcs molecule
                       
                """
                mcs_conflict = list(conflict)
                mcs_conflict.sort(reverse=True)

                # Editing the mcs molecule deleting all the selected conficting atoms
                edit_mcs_mol = Chem.EditableMol(mol)

                # WARNING: atom indexes are changed
                for i in mcs_conflict:
                    edit_mcs_mol.RemoveAtom(i)

                mcs_mol = edit_mcs_mol.GetMol()

                # The mcs molecule could be empty at this point
                if not mcs_mol.GetNumAtoms():
                    return mcs_mol

                # Deleting broken ring atoms if the atom rc > 0 and the atom is not
                # in a ring anymore
                mcs_conflict = [at.GetIdx() for at in mcs_mol.GetAtoms() if
                                int(at.GetProp('rc')) > 0 and not at.IsInRing()]

                mcs_conflict.sort(reverse=True)

                edit_mcs_mol = Chem.EditableMol(mcs_mol)
                # WARNING: atom indexes are changed
                for i in mcs_conflict:
                    edit_mcs_mol.RemoveAtom(i)

                mcs_mol = edit_mcs_mol.GetMol()

                # The mcs molecule could be empty at this point
                if not mcs_mol.GetNumAtoms():
                    return mcs_mol

                # Deleting eventually disconnected parts and keep the max fragment left
                fragments = Chem.rdmolops.GetMolFrags(mcs_mol)

                max_idx = 0
                lgt_max = 0

                for idx in range(0, len(fragments)):
                    lgt = len(fragments[idx])
                    if lgt > lgt_max:
                        lgt_max = lgt
                        max_idx = idx

                max_frag = fragments[max_idx]
                mcs_conflict = [at.GetIdx() for at in mcs_mol.GetAtoms() if not at.GetIdx() in max_frag]
                mcs_conflict.sort(reverse=True)
                edit_mcs_mol = Chem.EditableMol(mcs_mol)

                # WARNING: atom indexes have changed
                for i in mcs_conflict:
                    edit_mcs_mol.RemoveAtom(i)
                mcs_mol = edit_mcs_mol.GetMol()

                return mcs_mol

            mcs_conflict = set()
            for at in self.mcs_mol.GetAtoms():

                moli_idx = int(at.GetProp('to_moli'))
                molj_idx = int(at.GetProp('to_molj'))

                moli_idx_rc = int(self.__moli_noh.GetAtomWithIdx(moli_idx).GetProp('rc'))
                molj_idx_rc = int(self.__molj_noh.GetAtomWithIdx(molj_idx).GetProp('rc'))

                # Moli atom is a ring atom (rc>0) and its rc is different from 
                # the corresponding mcs rc atom  
                if moli_idx_rc > 0 and (moli_idx_rc != int(at.GetProp('rc'))):
                    if strict_flag:  # In strict mode we add the atom
                        mcs_conflict.add(at.GetIdx())
                    else:  # In loose mode we add the atom if it is not an aromatic atom
                        if not at.GetIsAromatic():
                            mcs_conflict.add(at.GetIdx())

                # Molj atom is a ring atom (rc>0) and its rc is different 
                # from the corresponding mcs rc atom 
                if molj_idx_rc > 0 and (molj_idx_rc != int(at.GetProp('rc'))):
                    if strict_flag:  # In strict mode we add the atom
                        mcs_conflict.add(at.GetIdx())
                    else:  # In loose mode we add the atom if it is not an aromatic atom
                        if not at.GetIsAromatic():
                            mcs_conflict.add(at.GetIdx())

            mcs_mol = extend_conflict(self.mcs_mol, mcs_conflict)

            return mcs_mol

        mcs_mol_copy = Chem.Mol(self.mcs_mol)
        orig_nha_mcs_mol = mcs_mol_copy.GetNumHeavyAtoms()

        # At this point the mcs_mol_copy has changed 
        mcs_mol_copy = delete_broken_ring()

        new_nha_mcs_mol = mcs_mol_copy.GetNumHeavyAtoms()

        scr_tmcsr =  math.exp(-2 * beta * (orig_nha_mcs_mol - new_nha_mcs_mol))
        print("tmcsr rule: orig nha is ",orig_nha_mcs_mol," new frag is",new_nha_mcs_mol,"delta",(orig_nha_mcs_mol - new_nha_mcs_mol),"score",scr_tmcsr)
        return scr_tmcsr

    # AtomicNumber rule 
    def atomic_number_rule(self,beta=0.1):

        """
        This rule checks how many elements have been changed in the MCS 
        and a score based on the fraction of MCS matches that are the same atomic number.
        When used with beta=0.1 and multiplied by mcsr, this is equivalent to counting
        mismatched atoms at only half weight.

        This has been extended to modify the amount of mismatch according to the 
        atoms being mapped. 
             
        """

        # A value of 0.5 is the same behaviour as before, a value of 1 means that the 
        # atoms are perfectly equivalent, a value of 0 means that the atoms are perfectly
        # non-equivalent (i.e the penalty should basically remove this atom pair from the
        # MCS). The default for pairs not in this data structure is 0.5. 
        # 
        # Note that we don't need the symmetry equivalent values: we will use the large of 
        # [i][j] and [j][i]
        transform_difficulty={ 
          # H to element - not sure this has any effect currently
          1: { 9: 0.5, 17: 0.25, 35: 0, 53: -0.5 },
          # O to element - methoxy to Cl/Br is easier than expected
          8: { 17: 0.85, 35: 0.85 },
          # F to element 
          9: { 17: 0.5, 35: 0.25, 53: 0 },
          # Cl to element 
          17: { 35: 0.85, 53: 0.65 },
          # Br to element
          35: { 53: 0.85 },
        }
        nmismatch=0
        for at in self.mcs_mol.GetAtoms():
            moli_idx = int(at.GetProp('to_moli'))
            molj_idx = int(at.GetProp('to_molj'))
            moli_a = self.__moli_noh.GetAtoms()[moli_idx]
            molj_a = self.__molj_noh.GetAtoms()[molj_idx]

            if moli_a.GetAtomicNum() != molj_a.GetAtomicNum():
                ij=-1
                ji=-1
                try:
                    ij=transform_difficulty[moli_a.GetAtomicNum()][molj_a.GetAtomicNum()]
                except KeyError:
                    pass
                try:
                    ji=transform_difficulty[molj_a.GetAtomicNum()][moli_a.GetAtomicNum()]
                except KeyError:
                    pass
                diff = max(ij,ji)
                if (diff==-1):
                    diff=0.5    # default for elements not found
                    
                nmismatch+=(1-diff)

        an_score =  math.exp(-1 * beta * nmismatch)
        return an_score

    # Sulfonamides rule
    def sulfonamides_rule(self):

        """
        This rule checks to see if we are growing a complete sulfonamide, and 
        returns 0 if we are. This means that if this rule is used we effectively disallow
        this transition. Testing has shown that growing -SO2NH2 from scratch performs
        very badly.
             
        """

        def adds_sulfonamide(mol):
            """
            Returns true if the removal of the MCS from the provided molecule
            leaves a sulfonamide
            """

            if not mol.HasSubstructMatch(self.mcs_mol):
                raise ValueError('RDkit MCS Subgraph molecule search failed in sulfonamide check')

            
            rwm=rdmolops.DeleteSubstructs(mol, self.mcs_mol)
            return rwm.HasSubstructMatch(Chem.MolFromSmarts('S(=O)(=O)N'))

        retval = 0 if (adds_sulfonamide(self.__moli_noh)) else 1
        retval = 0 if (adds_sulfonamide(self.__molj_noh)) else retval
        return retval

    # Heterocycles rule
    def heterocycles_rule(self):

        """
        This rule checks to see if we are growing a heterocycle from a hydrogen, and 
        returns 0 if we are. This means that if this rule is used we effectively disallow
        this transition. Testing has shown that growing a pyridine or other heterocycle
        is unlikely to work (better to grow phenyl then mutate)
             
        """

        def adds_heterocycle(mol):
            """
            Returns true if the removal of the MCS from the provided molecule
            leaves a sulfonamide
            """

            if not mol.HasSubstructMatch(self.mcs_mol):
                raise ValueError('RDkit MCS Subgraph molecule search failed in sulfonamide check')

            
            rwm=rdmolops.DeleteSubstructs(mol, self.mcs_mol)
            # Only picking up N/C containing heterocycles - odd cases like pyran derivatives are not caught
            grow6mheterocycle =  rwm.HasSubstructMatch(Chem.MolFromSmarts('[n]1[c,n][c,n][c,n][c,n][c,n]1'))

            # Note that growing pyrrole, furan or thiophene is allowed
            grow5mheterocycle =  rwm.HasSubstructMatch(Chem.MolFromSmarts('[o,n&X3,s]1[n][c,n][c,n][c,n]1'))
            grow5mheterocycle |=  rwm.HasSubstructMatch(Chem.MolFromSmarts('[o,n&X3,s]1[c,n][n][c,n][c,n]1'))
            return (grow6mheterocycle | grow5mheterocycle)



        retval = 0 if (adds_heterocycle(self.__moli_noh)) else 1
        retval = 0 if (adds_heterocycle(self.__molj_noh)) else retval
        return retval

    def transmuting_methyl_into_ring_rule(self):

        """
         Rule to prevent turning a methyl into a ring atom and similar transformations
         (you can grow a ring, but you can't transmute into one)

        """
        moli=self.__moli_noh
        molj=self.__molj_noh

        # Get list of bonds in mol i and j that go from the MCS to a non-MCS atom,
        # arranged in tuples with the index of the MCS atom
        moli_sub = moli.GetSubstructMatch(self.mcs_mol)
        molj_sub = molj.GetSubstructMatch(self.mcs_mol)

        is_bad=False

        for i in range(0,len(moli_sub)):
            edge_bondsi = [ b.GetBeginAtomIdx() for b in moli.GetBonds() if (b.GetEndAtomIdx()==moli_sub[i] and not b.GetBeginAtomIdx() in moli_sub) ]
            edge_bondsi += [ b.GetEndAtomIdx() for b in moli.GetBonds() if (b.GetBeginAtomIdx()==moli_sub[i] and not b.GetEndAtomIdx() in moli_sub) ]
            edge_bondsj = [ b.GetBeginAtomIdx() for b in molj.GetBonds() if (b.GetEndAtomIdx()==molj_sub[i] and not b.GetBeginAtomIdx() in molj_sub) ]
            edge_bondsj += [ b.GetEndAtomIdx() for b in molj.GetBonds() if (b.GetBeginAtomIdx()==molj_sub[i] and not b.GetEndAtomIdx() in molj_sub) ]
            #print("Atom",i,"index",moli_sub[i],"edge atoms on mol 1 are",edge_bondsi);
            #print("Atom",i,"index",molj_sub[i],"edge atoms on mol 2 are",edge_bondsj);

            for edgeAtom_i in edge_bondsi:
                for edgeAtom_j in edge_bondsj:
                    if (moli.GetAtomWithIdx(edgeAtom_i).IsInRing() ^ molj.GetAtomWithIdx(edgeAtom_j).IsInRing()):
                        is_bad=True

        return 0 if is_bad else 1

    def transmuting_halogen_into_alkyl_rule(self, beta=0.1):

        """
         Rule to prevent turning a halogen into an alkane chain: this seems to behave badly
         but it's not clear why

         Penalise by the equivalent of 2 atoms in the MCS for each mismatch

        """

        nmismatch = 0
        for at in self.mcs_mol.GetAtoms():
            moli_idx = int(at.GetProp('to_moli'))
            molj_idx = int(at.GetProp('to_molj'))
            moli_a = self.__moli_noh.GetAtoms()[moli_idx]
            molj_a = self.__molj_noh.GetAtoms()[molj_idx]

            if moli_a.GetAtomicNum() in [17,35,53] and molj_a.GetAtomicNum() == 6 and molj_a.GetDegree()>1:
                    nmismatch+=1
            if molj_a.GetAtomicNum() in [17,35,53] and moli_a.GetAtomicNum() == 6 and moli_a.GetDegree()>1:
                    nmismatch+=1

        hal_alk_score =  math.exp(-1 * beta * nmismatch * 2)
        return hal_alk_score

    def transmuting_ring_sizes_rule(self):

        """
         Rule to prevent turning a ring atom into a ring atom with a different ring size
         (you can grow a ring, but you can't turn a cyclopentyl into a cyclohexyl)

        """
        moli=self.__moli_noh
        molj=self.__molj_noh

        # Get list of bonds in mol i and j that go from the MCS to a non-MCS atom,
        # arranged in tuples with the index of the MCS atom
        moli_sub = moli.GetSubstructMatch(self.mcs_mol)
        molj_sub = molj.GetSubstructMatch(self.mcs_mol)

        is_bad=False

        for i in range(0,len(moli_sub)):
            edge_bondsi = [ b.GetBeginAtomIdx() for b in moli.GetBonds() if (b.GetEndAtomIdx()==moli_sub[i] and not b.GetBeginAtomIdx() in moli_sub) ]
            edge_bondsi += [ b.GetEndAtomIdx() for b in moli.GetBonds() if (b.GetBeginAtomIdx()==moli_sub[i] and not b.GetEndAtomIdx() in moli_sub) ]
            edge_bondsj = [ b.GetBeginAtomIdx() for b in molj.GetBonds() if (b.GetEndAtomIdx()==molj_sub[i] and not b.GetBeginAtomIdx() in molj_sub) ]
            edge_bondsj += [ b.GetEndAtomIdx() for b in molj.GetBonds() if (b.GetBeginAtomIdx()==molj_sub[i] and not b.GetEndAtomIdx() in molj_sub) ]
            #print("Atom",i,"index",moli_sub[i],"edge atoms on mol 1 are",edge_bondsi);
            #print("Atom",i,"index",molj_sub[i],"edge atoms on mol 2 are",edge_bondsj);

            for edgeAtom_i in edge_bondsi:
                for edgeAtom_j in edge_bondsj:
                    #print("Checking ring for atom",edgeAtom_i,edgeAtom_j,moli.GetAtomWithIdx(edgeAtom_i).IsInRing(),molj.GetAtomWithIdx(edgeAtom_j).IsInRing())
                    if (moli.GetAtomWithIdx(edgeAtom_i).IsInRing() and molj.GetAtomWithIdx(edgeAtom_j).IsInRing()):
                        for ring_size in range(3,8):
                            if (moli.GetAtomWithIdx(edgeAtom_i).IsInRingSize(ring_size) ^ molj.GetAtomWithIdx(edgeAtom_j).IsInRingSize(ring_size)):
                                is_bad=True
                            if (moli.GetAtomWithIdx(edgeAtom_i).IsInRingSize(ring_size) or molj.GetAtomWithIdx(edgeAtom_j).IsInRingSize(ring_size)):
                                break

        return 0 if is_bad else 1

    def heavy_atom_match_list(self):
        '''
        Returns a string listing the MCS match between the two molecules as 
          atom_m1:atom_m2,atom_m1:atom_m2,...
        Heavy atoms only
        '''
        maplist=[]
        for at in self.mcs_mol.GetAtoms():
            moli_idx = int(at.GetProp('to_moli'))
            molj_idx = int(at.GetProp('to_molj'))
            maplist.append(str(moli_idx)+":"+str(molj_idx))
        return ",".join(maplist)

    def all_atom_match_list(self):
        '''
        Returns a string listing the MCS match between the two molecules as 
          atom_m1:atom_m2,atom_m1:atom_m2,...
        All atoms including hydrogens
        '''
        def get_attached_hydrogens(mol,i):
            hydrogens = [ b.GetBeginAtomIdx() for b in mol.GetBonds() if b.GetEndAtomIdx()==i and mol.GetAtomWithIdx(b.GetBeginAtomIdx()).GetAtomicNum()==1 ]
            hydrogens += [ b.GetEndAtomIdx() for b in mol.GetBonds() if b.GetBeginAtomIdx()==i and mol.GetAtomWithIdx(b.GetEndAtomIdx()).GetAtomicNum()==1 ]
            return hydrogens

        def get_attached_atoms_not_in_mcs(mol,i):
            attached=[]
            for b in mol.GetBonds():
                if b.GetEndAtomIdx()==i or b.GetBeginAtomIdx()==i:
                    j=b.GetEndAtomIdx()
                    if (j==i):
                        j=b.GetBeginAtomIdx()
                    # OK, so j is the atom at the other end of the bond atom atom i. Is it in the MCS?
                    inMCS = mol.GetAtomWithIdx(j).HasProp('to_mcs')
                    if not inMCS:
                        attached.append(j)
            return attached


        moli=self.moli
        molj=self.molj

        maplist=[]

        # OK, this is painful, as the MCS only includes heavies. We could do this eficiently,
        # but the molecules are small so just brute force it
        for i in range(moli.GetNumAtoms()):
            # Is this atom in the MCS?
            mcslist = [ at for at in self.mcs_mol.GetAtoms() if int(at.GetProp('to_moli'))==i ]
            if (mcslist):
                j=int(mcslist[0].GetProp('to_molj'))
                hydindexi = get_attached_hydrogens(moli,i)
                hydindexj = get_attached_atoms_not_in_mcs(molj,j)
                for hmatch in zip(hydindexi,hydindexj):
                    maplist.append(str(hmatch[0])+":"+str(hmatch[1]))

        return self.heavy_atom_match_list()+","+",".join(maplist)
              
if "__main__" == __name__:

    mola = Chem.MolFromMolFile('../test/transforms/chlorophenyl.sdf', sanitize=False, removeHs=False)
    molb = Chem.MolFromMolFile('../test/transforms/methoxyphenyl.sdf', sanitize=False, removeHs=False)
    mola = Chem.MolFromMolFile('/home/mark/lomap-test/max/fxa/fxa_lig1.sdf', sanitize=False, removeHs=False)
    molb = Chem.MolFromMolFile('/home/mark/lomap-test/max/fxa/fxa_lig4.sdf', sanitize=False, removeHs=False)
    print("Mola: ",Chem.MolToSmiles(mola))
    print("Molb: ",Chem.MolToSmiles(molb))

    # MCS calculation
    try:
        MC = MCS(mola, molb, argparse.Namespace(time=20, verbose='info', max3d=15, threed=True))
        #MC = MCS(mola, molb)
    except Exception:
        raise ValueError('NO MCS FOUND......')

    # # Rules calculations
    mcsr = MC.mcsr()
    mncar = MC.mncar()
    atnum = MC.atomic_number_rule()

    strict = MC.tmcsr(strict_flag=True)
    loose = MC.tmcsr(strict_flag=False)

    print('TMCRS STRICT = %f , TMCRS LOOSE = %f' % (strict, loose))
    print('MCSR = ', mcsr)
    print('MNCAR = ', mncar)
    print('ATNUM = ', atnum)

    tmp = mcsr * mncar

    print('Total Strict = %f , Total Loose = %f' % (tmp * strict, tmp * loose))

    print('MCS is ',MC.mcs_mol.GetNumHeavyAtoms(),' ',Chem.MolToSmiles(MC.mcs_mol))
    for at in MC.mcs_mol.GetAtoms():
        moli_idx = int(at.GetProp('to_moli'))
        molj_idx = int(at.GetProp('to_molj'))
        moli_a = mola.GetAtoms()[moli_idx]
        molj_a = molb.GetAtoms()[molj_idx]
        print("MCS match: ",moli_idx,moli_a.GetAtomicNum(),molj_idx,molj_a.GetAtomicNum())

    print("sulfonamides:",MC.sulfonamides_rule())
    print("heterocycles:",MC.heterocycles_rule())
    print("growring:",MC.transmuting_methyl_into_ring_rule())
    print("changering:",MC.transmuting_ring_sizes_rule())
    print("transmuting_halogen_into_alkyl_rule:",MC.transmuting_halogen_into_alkyl_rule())
    print("transmuting_ring_sizes_rule:",MC.transmuting_ring_sizes_rule())
    print("Match list:",MC.heavy_atom_match_list())
    print("Match list:",MC.all_atom_match_list())

