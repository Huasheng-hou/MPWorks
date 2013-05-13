import datetime
import os
from pymongo import MongoClient, DESCENDING
from fireworks.utilities.fw_serializers import FWSerializable
from mpworks.snl_utils.mpsnl import MPStructureNL, SNLGroup
from pymatgen.symmetry.finder import SymmetryFinder


__author__ = 'Anubhav Jain'
__copyright__ = 'Copyright 2013, The Materials Project'
__version__ = '0.1'
__maintainer__ = 'Anubhav Jain'
__email__ = 'ajain@lbl.gov'
__date__ = 'Apr 24, 2013'

# Parameters for spacegroup and mps_unique_id determination
SPACEGROUP_TOLERANCE = 0.1  # as suggested by Shyue, 6/19/2012


class SNLMongoAdapter(FWSerializable):
    def __init__(self, host='localhost', port=27017, db='snl', username=None,
                 password=None):
        self.host = host
        self.port = port
        self.db = db
        self.username = username
        self.password = password

        self.connection = MongoClient(host, port, j=False)
        self.database = self.connection[db]
        if self.username:
            self.database.authenticate(username, password)

        self.snl = self.database.snl
        self.snlgroups = self.database.snlgroups
        self.id_assigner = self.database.id_assigner

        self._update_indices()

    def _reset(self):
        self.restart_id_assigner_at(1, 1)
        self.snl.remove()
        self.snlgroups.remove()

    def _update_indices(self):
        self.snl.ensure_index('snl_id', unique=True)
        self.snl.ensure_index('autometa.nsites')
        self.snl.ensure_index('autometa.nelements')
        self.snl.ensure_index('autometa.nlements')
        self.snl.ensure_index('autometa.formula')
        self.snl.ensure_index('autometa.reduced_cell_formula')
        self.snl.ensure_index('autometa.reduced_cell_formula_abc')
        self.snl.ensure_index('autometa.is_ordered')

        self.snlgroups.ensure_index('snlgroup_id', unique=True)
        self.snlgroups.ensure_index('all_snl_ids')
        self.snlgroups.ensure_index('canonical_snl.snl_id')
        self.snlgroups.ensure_index('autometa.nsites')
        self.snlgroups.ensure_index('autometa.nelements')
        self.snlgroups.ensure_index('autometa.nlements')
        self.snlgroups.ensure_index('autometa.formula')
        self.snlgroups.ensure_index('autometa.reduced_cell_formula')
        self.snlgroups.ensure_index('autometa.reduced_cell_formula_abc')
        self.snlgroups.ensure_index('autometa.is_ordered')

    def _get_next_snl_id(self):
        snl_id = self.id_assigner.find_and_modify(
            query={}, update={'$inc': {'next_snl_id': 1}})['next_snl_id']
        return snl_id

    def _get_next_snlgroup_id(self):
        snlgroup_id = self.id_assigner.find_and_modify(
            query={},
            update={'$inc': {'next_snlgroup_id': 1}})['next_snlgroup_id']
        return snlgroup_id

    def restart_id_assigner_at(self, next_snl_id, next_snlgroup_id):
        self.id_assigner.remove()
        self.id_assigner.insert(
            {"next_snl_id": next_snl_id, "next_snlgroup_id": next_snlgroup_id})

    def add_snl(self, snl, force_new=False, snlgroup_guess=None):
        snl_id = self._get_next_snl_id()
        sf = SymmetryFinder(snl.structure, SPACEGROUP_TOLERANCE)
        sf.get_spacegroup()
        sgnum = sf.get_spacegroup_number() if sf.get_spacegroup_number() \
            else -1
        sgsym = sf.get_spacegroup_symbol() if sf.get_spacegroup_symbol() \
            else 'unknown'
        sghall = sf.get_hall() if sf.get_hall() else 'unknown'
        sgxtal = sf.get_crystal_system() if sf.get_crystal_system() \
            else 'unknown'
        sglatt = sf.get_lattice_type() if sf.get_lattice_type() else 'unknown'
        sgpoint = unicode(sf.get_point_group(), errors="ignore")

        mpsnl = MPStructureNL.from_snl(snl, snl_id, sgnum, sgsym, sghall,
                                       sgxtal, sglatt, sgpoint)
        snlgroup, add_new = self.add_mpsnl(mpsnl, force_new, snlgroup_guess)
        return mpsnl, snlgroup.snlgroup_id

    def add_mpsnl(self, mpsnl, force_new=False, snlgroup_guess=None):
        snl_d = mpsnl.to_dict
        snl_d['snl_timestamp'] = datetime.datetime.utcnow().isoformat()
        self.snl.insert(snl_d)
        return self.build_groups(mpsnl, force_new, snlgroup_guess)

    def _add_if_belongs(self, snlgroup, mpsnl, testing_mode):
        if snlgroup.add_if_belongs(mpsnl):
            print 'MATCH FOUND, grouping (snl_id, snlgroup): {}'.format((mpsnl.snl_id, snlgroup.snlgroup_id))
            if not testing_mode:
                self.snlgroups.update({'snlgroup_id': snlgroup.snlgroup_id}, snlgroup.to_dict)
            return True
        return False

    def build_groups(self, mpsnl, force_new=False, snlgroup_guess=None, testing_mode=False):
        # testing mode is used to see if something already exists in DB w/o adding it to the db

        match_found = False
        if not force_new:
            if snlgroup_guess:
                sgp = self.snlgroups.find_one({'snlgroup_id': snlgroup_guess})
                snlgroup = SNLGroup.from_dict(sgp)
                match_found = self._add_if_belongs(snlgroup, mpsnl, testing_mode)

            if not match_found:
                # look at all potential matches
                for entry in self.snlgroups.find({'snlgroup_key': mpsnl.snlgroup_key},
                                                 sort=[("num_snl", DESCENDING)]):
                    snlgroup = SNLGroup.from_dict(entry)
                    match_found = self._add_if_belongs(snlgroup, mpsnl, testing_mode)
                    if match_found:
                        break

        if not match_found:
            # add a new SNLGroup
            snlgroup_id = self._get_next_snlgroup_id()
            snlgroup = SNLGroup(snlgroup_id, mpsnl)
            if not testing_mode:
                self.snlgroups.insert(snlgroup.to_dict)

        return snlgroup, not match_found

    def to_dict(self):
        """
        Note: usernames/passwords are exported as unencrypted Strings!
        """
        return {'host': self.host, 'port': self.port, 'db': self.db,
                'username': self.username, 'password': self.password}

    @classmethod
    def from_dict(cls, d):
        return SNLMongoAdapter(d['host'], d['port'], d['db'], d['username'],
                               d['password'])

    @classmethod
    def auto_load(cls):
        s_dir = os.environ['DB_LOC']
        s_file = os.path.join(s_dir, 'snl_db.yaml')
        return SNLMongoAdapter.from_file(s_file)