"""Native rows are confined to the selected manifest scope before staging."""
import copy
import unittest
from codex_migrate.compatibility import STATE_TABLES,HISTORY_TABLES
from codex_migrate.errors import MigrationError
from codex_migrate.staging import validate_import_scope


class ImportScopeTests(unittest.TestCase):
    def setUp(self):
        self.manifest={'projects':[{'id':'synthetic-project'}],'threads':[{'id':'synthetic-thread'}]}
        self.state={table:[] for table in STATE_TABLES};self.history={table:[] for table in HISTORY_TABLES}
        self.state['projects']=[{'id':'synthetic-project'}]
        self.state['project_roots']=[{'project_id':'synthetic-project','path':'E:/synthetic'}]
        self.state['threads']=[{'id':'synthetic-thread','project_id':'synthetic-project','thread_section_id':'synthetic-section'}]
        self.state['thread_sections']=[{'id':'synthetic-section'}]
        self.history['thread_history_projection_state']=[{'thread_id':'synthetic-thread','next_rollout_ordinal':987}]
    def test_valid_scope_preserves_rows_and_does_not_interpret_ordinals(self):
        before=copy.deepcopy((self.state,self.history,self.manifest))
        self.assertIsNone(validate_import_scope(self.state,self.history,self.manifest))
        self.assertEqual((self.state,self.history,self.manifest),before)
    def test_unassigned_chat_needs_no_fake_project(self):
        self.manifest['projects']=[];self.state['projects']=[];self.state['project_roots']=[];self.state['threads'][0]['project_id']=None
        validate_import_scope(self.state,self.history,self.manifest)
    def test_extra_missing_or_duplicate_main_identities_are_refused(self):
        for table in ('projects','threads'):
            for mutation in ('extra','missing','duplicate'):
                with self.subTest(table=table,mutation=mutation):
                    state=copy.deepcopy(self.state)
                    if mutation=='extra': state[table].append({'id':'synthetic-outside'})
                    elif mutation=='missing': state[table]=[]
                    else: state[table].append(dict(state[table][0]))
                    with self.assertRaises(MigrationError): validate_import_scope(state,self.history,self.manifest)
    def test_every_state_and_history_thread_relation_is_confined(self):
        for table in ('thread_dynamic_tools','thread_attachments','thread_read_receipts','guardian_review_feedback'):
            with self.subTest(table=table):
                state=copy.deepcopy(self.state);state[table]=[{'thread_id':'synthetic-unselected-target'}]
                with self.assertRaises(MigrationError): validate_import_scope(state,self.history,self.manifest)
        for table in HISTORY_TABLES:
            with self.subTest(table=table):
                history=copy.deepcopy(self.history);history[table]=[{'thread_id':'synthetic-unselected-target'}]
                with self.assertRaises(MigrationError): validate_import_scope(self.state,history,self.manifest)
        for key in ('parent_thread_id','child_thread_id'):
            state=copy.deepcopy(self.state);edge={'parent_thread_id':'synthetic-thread','child_thread_id':'synthetic-thread'};edge[key]='synthetic-outside';state['thread_spawn_edges']=[edge]
            with self.assertRaises(MigrationError): validate_import_scope(state,self.history,self.manifest)
    def test_projects_and_sections_cannot_refer_to_unrelated_target_objects(self):
        for mutate in (lambda s:s['threads'][0].update(project_id='target-existing-project'),lambda s:s['project_roots'][0].update(project_id='target-existing-project'),lambda s:s['thread_sections'].append({'id':'unreferenced-section'}),lambda s:s.update(thread_sections=[])):
            state=copy.deepcopy(self.state);mutate(state)
            with self.assertRaises(MigrationError): validate_import_scope(state,self.history,self.manifest)
    def test_unknown_tables_and_malformed_relations_are_refused(self):
        state=copy.deepcopy(self.state);state['unapproved_table']=[]
        with self.assertRaises(MigrationError): validate_import_scope(state,self.history,self.manifest)
        history=copy.deepcopy(self.history);history['thread_realtime_items']=[{'item_id':'missing-thread-id'}]
        with self.assertRaises(MigrationError): validate_import_scope(self.state,history,self.manifest)

if __name__=='__main__': unittest.main()
