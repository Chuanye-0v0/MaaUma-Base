"""Ordinary inheritance options only; no OCR, private paths or device I/O."""
import importlib.util
from pathlib import Path
import unittest

SOURCE=Path(__file__).resolve().parents[1]/'backend/core/autonomous_training/plant_inheritance.py'
spec=importlib.util.spec_from_file_location('ordinary_inheritance',SOURCE)
inheritance=importlib.util.module_from_spec(spec);spec.loader.exec_module(inheritance)

class InheritanceOptions(unittest.TestCase):
    def test_missing_legacy_options_are_not_written_as_new_defaults(self):
        self.assertEqual(inheritance.validate({}),{})

    def test_borrow_preference_is_retained_in_both_modes(self):
        for mode in ('keep','auto'):
            value={'inheritance_selection':mode,'inheritance_borrow_friend':False}
            self.assertEqual(inheritance.validate(value),value)

    def test_complete_keep_does_not_replace_current_pair(self):
        context={'mode':'start','inheritance_options':{'inheritance_selection':'keep'}}
        self.assertEqual(inheritance.choose({'page':'inheritance','inheritance_full':True},context),'continue_inheritance')

    def test_explicit_auto_is_not_swallowed_by_current_pair(self):
        context={'mode':'start','inheritance_options':{'inheritance_selection':'auto'}}
        self.assertEqual(inheritance.choose({'page':'inheritance','inheritance_full':True},context),'auto_inheritance')

if __name__=='__main__':unittest.main()
