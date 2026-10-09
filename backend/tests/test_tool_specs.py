import json
import unittest
from agent import TOOLS
from tool_contract import parse_tool_arguments,ToolArgumentError

class ToolSpecsTests(unittest.TestCase):
    def parse(self,name,args):
        return parse_tool_arguments({'function':{'name':name,'arguments':json.dumps(args)}},TOOLS)

    def test_all_tools_reject_unknown_fields_and_only_bound_control_strings(self):
        for tool in TOOLS:
            schema=tool['function']['parameters']
            self.assertFalse(schema['additionalProperties'])
            with self.assertRaises(ToolArgumentError):self.parse(tool['function']['name'],{'unexpected':1})
            def check(node, field=''):
                if node.get('type')=='string':
                    if field in ('content', 'old', 'new', 'patch'):
                        self.assertNotIn('maxLength',node)
                        self.assertTrue(node['x-noNul'])
                    else:self.assertIn('maxLength',node)
                for field,child in node.get('properties',{}).items():check(child,field)
                if 'items' in node:check(node['items'],field)
                for child in node.get('oneOf',[]):check(child,field)
            check(schema)

    def test_strings_bytes_and_empty_content(self):
        for name,args in [('run_shell',{'command':' '*3}),('run_shell',{'command':'x'*2001}),('write_file',{'path':'x','content':'text\x00'}),('replace_in_file',{'path':'x','old':'','new':''}),('search_code',{'query':' '}),('read_file',{'path':'x\x00'})]:
            with self.assertRaises(ToolArgumentError):self.parse(name,args)
        self.assertEqual(self.parse('write_file',{'path':'empty.txt','content':''})['content'],'')
        content = '中' * 100000
        self.assertEqual(self.parse('write_file',{'path':'x','content':content})['content'], content)
        self.parse('replace_in_file',{'path':'x','old':content,'new':content + 'changed'})

    def test_wrong_types_ranges_and_action_specific_keys(self):
        for name,args in [('read_file',{'path':'x','offset':-1}),('read_file',{'path':'x','limit':True}),('read_code',{'path':'x','start_line':0}),('http_request',{'path':'/','expect_status':600}),('http_request',{'path':'https://example.test','expect_status':200}),('browser_check',{'actions':[]}),('browser_check',{'actions':[{'action':'reload','value':'ignored'}]}),('read_document',{'id':'filename.txt'}),('run_build',{'requirement_ids':['R1','R1']})]:
            with self.assertRaises(ToolArgumentError):self.parse(name,args)
        self.parse('http_request',{'path':'/api?a=1','expect_status':200,'body':{'arbitrary':'json'}})
        self.parse('browser_check',{'actions':[{'action':'fill','selector':'input','value':''}]})
        self.parse('browser_check',{'actions':[{'action':'click','selector':'#delete','dialog':'accept'}]})
        with self.assertRaises(ToolArgumentError):self.parse('browser_check',{'actions':[{'action':'fill','selector':'input','value':'x','dialog':'accept'}]})

    def test_json_duplicates_and_nonfinite_numbers_are_rejected(self):
        for raw in ['{"command":"echo a","command":"echo b"}','{"path":"/","expect_status":200,"body":NaN}']:
            with self.assertRaises(ToolArgumentError):parse_tool_arguments({'function':{'name':'run_shell' if 'command' in raw else 'http_request','arguments':raw}},TOOLS)

    def test_model_browser_setup_executes_without_claiming_requirement_acceptance(self):
        args={'actions':[{'action':'fill','selector':'#email','value':'qa@example.com'}], 'requirement_ids':['R1']}
        call={'function':{'name':'browser_check','arguments':json.dumps(args)}}
        with self.assertRaises(ToolArgumentError):parse_tool_arguments(call,TOOLS)
        adjustments=[]
        parsed=parse_tool_arguments(call,TOOLS,adjustments=adjustments)
        self.assertEqual(parsed['requirement_ids'],[])
        self.assertEqual(parsed['actions'],args['actions'])
        self.assertIn('准备',adjustments[0]['reason'])
        call['function']['arguments']=json.dumps({'actions':[{'action':'fill','selector':'#email','value':42}],'requirement_ids':['R1']})
        with self.assertRaises(ToolArgumentError):parse_tool_arguments(call,TOOLS,adjustments=[])
