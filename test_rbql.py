#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import unicode_literals
from __future__ import print_function

import sys
import os
import argparse
import random
import unittest
import re
import tempfile
import time
import importlib
import codecs
import io
import subprocess

import rbql
from rbql import rbql_utils

#This module must be both python2 and python3 compatible


default_csv_encoding = rbql.default_csv_encoding
script_dir = os.path.dirname(os.path.abspath(__file__))
tmp_dir = tempfile.gettempdir()

line_separators = ['\n', '\r\n', '\r']

TEST_JS = True
#TEST_JS = False #DBG


def unquote_field(field):
    field_rgx_external_whitespaces = re.compile('^ *"((?:[^"]*"")*[^"]*)" *$')
    match_obj = field_rgx_external_whitespaces.match(field)
    if match_obj is not None:
        return match_obj.group(1).replace('""', '"')
    return field


def unquote_fields(fields):
    return [unquote_field(f) for f in fields]


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def write_index(records, index_path):
    with open(index_path, 'w') as f:
        for record in records:
            f.write('\t'.join(record) + '\n')


def update_index(index_path, new_record, index_max_size):
    records = rbql.try_read_index(index_path)
    records = [rec for rec in records if not len(rec) or rec[0] != new_record[0]]
    records.append(new_record)
    if len(records) > index_max_size:
        del records[0]
    write_index(records, index_path)


def stochastic_quote_field(src, delim):
    if src.find('"') != -1 or src.find(delim) != -1 or random.randint(0, 1) == 1:
        spaces_before = ' ' * random.randint(0, 2) if delim != ' ' else ''
        spaces_after = ' ' * random.randint(0, 2) if delim != ' ' else ''
        escaped = src.replace('"', '""')
        escaped = '{}"{}"{}'.format(spaces_before, escaped, spaces_after)
        return escaped
    return src


def quote_field(src, delim):
    if src.find('"') != -1 or src.find(delim) != -1:
        escaped = src.replace('"', '""')
        escaped = '"{}"'.format(escaped)
        return escaped
    return src


def quoted_join(fields, delim):
    return delim.join([stochastic_quote_field(f, delim) for f in fields])


def whitespace_join(fields):
    result = ' ' * random.randint(0, 5)
    for f in fields:
        result += f + ' ' * random.randint(1, 5)
    return result


def smart_join(fields, dlm, policy):
    if policy == 'simple':
        return dlm.join(fields)
    elif policy == 'whitespace':
        assert dlm == ' '
        return whitespace_join(fields)
    elif policy == 'quoted':
        assert dlm != '"'
        return quoted_join(fields, dlm)
    elif policy == 'monocolumn':
        assert len(fields) == 1
        return fields[0]
    else:
        raise RuntimeError('Unknown policy')


def smart_split(src, dlm, policy):
    if policy == 'monocolumn':
        return [src]
    if policy == 'simple':
        return src.split(dlm)
    assert policy == 'quoted'
    res = rbql_utils.split_quoted_str(src, dlm)[0]
    res_preserved = rbql_utils.split_quoted_str(src, dlm, True)[0]
    assert dlm.join(res_preserved) == src
    assert res == unquote_fields(res_preserved)
    return res



def table_to_string(array2d, delim, policy):
    line_separator = random.choice(line_separators)
    result = line_separator.join([smart_join(row, delim, policy) for row in array2d])
    if len(array2d):
        result += line_separator
    return result


def table_to_file(array2d, dst_path, delim, policy):
    with codecs.open(dst_path, 'w', 'latin-1') as f:
        for row in array2d:
            f.write(smart_join(row, delim, policy))
            f.write(random.choice(line_separators))


def table_to_stream(array2d, delim, policy):
    return io.StringIO(table_to_string(array2d, delim, policy))


rainbow_ut_prefix = 'ut_rbconvert_'


def run_file_query_test_py(query, input_path, testname, delim, policy, csv_encoding):
    dst_table_filename = '{}.{}.{}.tsv'.format(testname, time.time(), random.randint(1, 1000000))
    output_path = os.path.join(tmp_dir, dst_table_filename)
    with rbql.RbqlPyEnv() as worker_env:
        tmp_path = worker_env.module_path
        rbql.parse_to_py([query], tmp_path, delim, policy, '\t', 'simple', csv_encoding, None)
        rbconvert = worker_env.import_worker()
        warnings = None
        with codecs.open(input_path, encoding=csv_encoding) as src, codecs.open(output_path, 'w', encoding=csv_encoding) as dst:
            warnings = rbconvert.rb_transform(src, dst)

        assert os.path.exists(tmp_path)
        worker_env.remove_env_dir()
        assert not os.path.exists(tmp_path)
    return (output_path, warnings)


def table_has_delim(array2d, delim):
    for r in array2d:
        for c in r: 
            if c.find(delim) != -1:
                return True
    return False


def parse_json_report(exit_code, err_data):
    err_data = err_data.decode('latin-1')
    if not len(err_data) and exit_code == 0:
        return dict()
    try:
        import json
        report = json.loads(err_data)
        if exit_code != 0 and 'error' not in report:
            report['error'] = 'Unknown error'
        return report
    except Exception:
        err_msg = err_data if len(err_data) else 'Unknown error'
        report = {'error': err_msg}
        return report


def run_conversion_test_py(query, input_table, testname, input_delim, input_policy, output_delim, output_policy, custom_init_path=None, join_csv_encoding=default_csv_encoding):
    with rbql.RbqlPyEnv() as worker_env:
        tmp_path = worker_env.module_path
        src = table_to_stream(input_table, input_delim, input_policy)
        dst = io.StringIO()
        rbql.parse_to_py([query], tmp_path, input_delim, input_policy, output_delim, output_policy, join_csv_encoding, custom_init_path)
        assert os.path.isfile(tmp_path) and os.access(tmp_path, os.R_OK)
        rbconvert = worker_env.import_worker()
        warnings = rbconvert.rb_transform(src, dst)
        out_data = dst.getvalue()
        if len(out_data):
            out_lines = out_data[:-1].split('\n')
            out_table = [smart_split(ln, output_delim, output_policy) for ln in out_lines]
        else:
            out_table = []
        assert os.path.exists(tmp_path)
        worker_env.remove_env_dir()
        assert not os.path.exists(tmp_path)
        return (out_table, warnings)


def run_file_query_test_js(query, input_path, testname, delim, policy, csv_encoding, out_format):
    rnd_string = '{}{}_{}_{}'.format(rainbow_ut_prefix, time.time(), testname, random.randint(1, 100000000)).replace('.', '_')
    dst_table_filename = '{}.tsv'.format(rnd_string)
    output_path = os.path.join(tmp_dir, dst_table_filename)
    assert not os.path.exists(output_path)
    cli_rbql_js_path = os.path.join(script_dir, 'rbql-js', 'cli_rbql.js')

    cmd = ['node', cli_rbql_js_path, '--delim', delim, '--policy', policy, '--input', input_path, '--encoding', csv_encoding, '--query', query.encode('utf-8'), '--output', output_path, '--error-format', 'json']
    if out_format is not None:
        cmd += ['--out-format', out_format]
    pobj = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out_data, err_data = pobj.communicate()
    exit_code = pobj.returncode

    operation_report = parse_json_report(exit_code, err_data)
    warnings = operation_report.get('warnings')
    operation_error = operation_report.get('error')
    if operation_error is not None:
        raise RuntimeError("Error in file test: {}.\nError text:\n{}\n".format(testname, operation_error))
    return (output_path, warnings)


def run_conversion_test_js(query, input_table, testname, input_delim, input_policy, output_delim, output_policy, csv_encoding=default_csv_encoding, custom_init_path=None):
    cli_rbql_js_path = os.path.join(script_dir, 'rbql-js', 'cli_rbql.js')
    src = table_to_string(input_table, input_delim, input_policy)
    cmd = ['node', cli_rbql_js_path, '--delim', input_delim, '--policy', input_policy, '--encoding', csv_encoding, '--query', query.encode('utf-8'), '--out-delim', output_delim, '--out-policy', output_policy, '--error-format', 'json']
    if custom_init_path is not None:
        cmd += ['--init-source-file', custom_init_path]

    pobj = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE)
    out_data, err_data = pobj.communicate(src.encode(csv_encoding))
    exit_code = pobj.returncode

    operation_report = parse_json_report(exit_code, err_data)
    warnings = operation_report.get('warnings')
    operation_error = operation_report.get('error')
    if operation_error is not None:
        raise RuntimeError("Error in file test: {}.\nError text:\n{}\n".format(testname, operation_error))

    out_table = []
    out_data = out_data.decode(csv_encoding)
    if len(out_data):
        out_lines = out_data[:-1].split('\n')
        out_table = [smart_split(ln, output_delim, output_policy) for ln in out_lines]
    return (out_table, warnings)


def make_random_csv_entry(min_len, max_len, restricted_chars):
    strlen = random.randint(min_len, max_len)
    char_set = list(range(256))
    restricted_chars = [ord(c) for c in restricted_chars]
    char_set = [c for c in char_set if c not in restricted_chars]
    data = list()
    for i in rbql.xrange6(strlen):
        data.append(random.choice(char_set))
    pseudo_latin = bytes(bytearray(data)).decode('latin-1')
    return pseudo_latin


def generate_random_scenario(max_num_rows, max_num_cols, delims):
    num_rows = random.randint(1, max_num_rows)
    num_cols = random.randint(1, max_num_cols)
    delim = random.choice(delims)
    policy = random.choice(['simple', 'quoted'])
    restricted_chars = ['\r', '\n', '\t']
    if policy == 'simple':
        restricted_chars.append(delim)
    key_col = random.randint(0, num_cols - 1)
    good_keys = ['Hello', 'Avada Kedavra ', ' ??????', '128', '3q295 fa#(@*$*)', ' abc defg ', 'NR', 'a1', 'a2']
    input_table = list()
    for r in rbql.xrange6(num_rows):
        input_table.append(list())
        for c in rbql.xrange6(num_cols):
            if c != key_col:
                input_table[-1].append(make_random_csv_entry(0, 20, restricted_chars))
            else:
                input_table[-1].append(random.choice(good_keys))

    canonic_table = list()
    target_key = random.choice(good_keys)
    if random.choice([True, False]):
        sql_op = '!='
        canonic_table = [row[:] for row in input_table if row[key_col] != target_key]
    else:
        sql_op = '=='
        canonic_table = [row[:] for row in input_table if row[key_col] == target_key]
    query = 'select * where a{} {} "{}"'.format(key_col + 1, sql_op, target_key)

    return (input_table, query, canonic_table, delim, policy)



def compare_warnings(tester, canonic_warnings, test_warnings):
    if test_warnings is None:
        tester.assertTrue(canonic_warnings is None)
        return
    if canonic_warnings is None:
        canonic_warnings = list()
    canonic_warnings = sorted(canonic_warnings)
    test_warnings = sorted(test_warnings.keys())
    tester.assertEqual(canonic_warnings, test_warnings)


def get_random_output_format():
    if random.choice([True, False]):
        return (',', 'quoted')
    return ('\t', 'simple')


def select_random_formats(input_table, allowed_delims='aA8 !#$%&\'()*+,-./:;<=>?@[\]^_`{|}~\t'):
    input_delim = random.choice(allowed_delims)
    if table_has_delim(input_table, input_delim):
        input_policy = 'quoted'
    else:
        input_policy = random.choice(['quoted', 'simple'])
    output_delim, output_policy = get_random_output_format()
    return (input_delim, input_policy, output_delim, output_policy)



class TestEverything(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        old_unused = [f for f in os.listdir(tmp_dir) if f.startswith(rainbow_ut_prefix)]
        for name in old_unused:
            script_path = os.path.join(tmp_dir, name)
            os.remove(script_path)


    def compare_tables(self, canonic_table, test_table):
        self.assertEqual(len(canonic_table), len(test_table))
        for i in rbql.xrange6(len(canonic_table)):
            self.assertEqual(len(canonic_table[i]), len(test_table[i]))
            self.assertEqual(canonic_table[i], test_table[i])
        self.assertEqual(canonic_table, test_table)


    def test_random_bin_tables(self):
        test_name = 'test_random_bin_tables'
        for subtest in rbql.xrange6(20):
            input_table, query, canonic_table, input_delim, input_policy = generate_random_scenario(200, 6, ['\t', ',', ';', '|'])
            output_delim = '\t'
            output_policy = 'simple'

            test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)

            if TEST_JS:
                test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
                self.compare_tables(canonic_table, test_table)


    def test_run1(self):
        test_name = 'test1'

        input_table = list()
        input_table.append(['5', 'haha', 'hoho'])
        input_table.append(['-20', 'haha', 'hioho'])
        input_table.append(['50', 'haha', 'dfdf'])
        input_table.append(['20', 'haha', ''])

        canonic_table = list()
        canonic_table.append(['3', '50', '4'])
        canonic_table.append(['4', '20', '0'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = 'select NR, a1, len(a3) where int(a1) > 5'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = 'select NR, a1, a3.length where a1 > 5'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run2(self):
        test_name = 'test2'

        input_table = list()
        input_table.append(['5', 'haha', 'hoho'])
        input_table.append(['-20', 'haha', 'hioho'])
        input_table.append(['50', 'haha', 'dfdf'])
        input_table.append(['20', 'haha', ''])
        input_table.append(['8'])
        input_table.append(['3', '4', '1000', 'asdfasf', 'asdfsaf', 'asdfa'])
        input_table.append(['11', 'hoho', ''])
        input_table.append(['10', 'hihi', ''])
        input_table.append(['13', 'haha', ''])

        canonic_table = list()
        canonic_table.append(['haha'])
        canonic_table.append(['hoho'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = '\tselect    distinct\ta2 where int(a1) > 10 '
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, ['input_fields_info'], warnings)

        if TEST_JS:
            query = '\tselect    distinct\ta2 where a1 > 10  '
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, ['input_fields_info'], warnings)


    def test_run4(self):
        test_name = 'test4'
        input_table = list()
        input_table.append(['0', 'haha', 'hoho'])
        input_table.append(['9'])
        input_table.append(['81', 'haha', 'dfdf'])
        input_table.append(['4', 'haha', 'dfdf', 'asdfa', '111'])

        canonic_table = list()
        canonic_table.append(['0', r"\'\"a1   bc"])
        canonic_table.append(['3', r"\'\"a1   bc"])
        canonic_table.append(['9', r"\'\"a1   bc"])
        canonic_table.append(['2', r"\'\"a1   bc"])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = r'select int(math.sqrt(int(a1))), r"\'\"a1   bc"'
        with tempfile.NamedTemporaryFile() as init_tmp_file:
            with open(init_tmp_file.name, 'w') as tf:
                tf.write('import math\nimport os\n')
            test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy, custom_init_path=init_tmp_file.name)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, ['input_fields_info'], warnings)

        if TEST_JS:
            query = r'select Math.floor(Math.sqrt(a1)), String.raw`\'\"a1   bc`'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, ['input_fields_info'], warnings)


    #TODO add test with js regex with multiple spaces and check that it is preserved during parsing

    def test_run5(self):
        test_name = 'test5'
        query = 'select a2'
        input_table = list()
        input_table.append(['0', 'haha', 'hoho'])
        input_table.append(['9'])
        input_table.append(['81', 'haha', 'dfdf'])
        input_table.append(['4', 'haha', 'dfdf', 'asdfa', '111'])

        canonic_table = list()
        canonic_table.append(['haha'])
        canonic_table.append([''])
        canonic_table.append(['haha'])
        canonic_table.append(['haha'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)


        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, ['input_fields_info', 'null_value_in_output'], warnings)

        if TEST_JS:
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, ['input_fields_info', 'null_value_in_output'], warnings)


    def test_run6(self):
        test_name = 'test6'

        input_table = list()
        input_table.append(['5', 'car', 'lada'])
        input_table.append(['-20', 'car', 'Ferrari'])
        input_table.append(['50', 'plane', 'tu-134'])
        input_table.append(['20', 'boat', 'destroyer'])
        input_table.append(['10', 'boat', 'yacht '])
        input_table.append(['200', 'plane', 'boeing 737'])
        input_table.append(['80', 'train', 'Thomas'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        join_table = list()
        join_table.append(['bicycle', 'legs'])
        join_table.append(['car', 'gas '])
        join_table.append(['plane', 'wings  '])
        join_table.append(['boat', 'wind'])
        join_table.append(['rocket', 'some stuff'])

        join_delim = ';'
        join_policy = 'simple'
        join_table_path = os.path.join(tempfile.gettempdir(), '{}_rhs_join_table.tsv'.format(test_name))
        table_to_file(join_table, join_table_path, join_delim, join_policy)
        update_index(rbql.table_index_path, [join_table_path, join_delim, join_policy, ''], 100)

        canonic_table = list()
        canonic_table.append(['5', '10', 'boat', 'yacht ', 'boat', 'wind'])
        canonic_table.append(['4', '20', 'boat', 'destroyer', 'boat', 'wind'])
        canonic_table.append(['2', '-20', 'car', 'Ferrari', 'car', 'gas '])
        canonic_table.append(['1', '5', 'car', 'lada', 'car', 'gas '])
        canonic_table.append(['3', '50', 'plane', 'tu-134', 'plane', 'wings  '])
        canonic_table.append(['6', '200', 'plane', 'boeing 737', 'plane', 'wings  '])

        query = r'select NR, * inner join {} on a2 == b1 where b2 != "haha" and int(a1) > -100 and len(b2) > 1 order by a2, int(a1)'.format(join_table_path)
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None,  warnings)

        if TEST_JS:
            query = r'select NR, * inner join {} on a2 == b1 where   b2 !=  "haha" &&  a1 > -100 &&  b2.length >  1 order by a2, parseInt(a1)'.format(join_table_path)
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run7(self):
        test_name = 'test7'

        input_table = list()
        input_table.append(['100', 'magic carpet', 'nimbus 3000'])
        input_table.append(['5', 'car', 'lada'])
        input_table.append(['-20', 'car', 'ferrari'])
        input_table.append(['50', 'plane', 'tu-134'])
        input_table.append(['20', 'boat', 'destroyer'])
        input_table.append(['10', 'boat', 'yacht'])
        input_table.append(['200', 'plane', 'boeing 737'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        join_table = list()
        join_table.append(['bicycle', 'legs'])
        join_table.append(['car', 'gas'])
        join_table.append(['plane', 'wings'])
        join_table.append(['rocket', 'some stuff'])

        join_table_path = os.path.join(tempfile.gettempdir(), '{}_rhs_join_table.tsv'.format(test_name))
        table_to_file(join_table, join_table_path, input_delim, input_policy)

        canonic_table = list()
        canonic_table.append(['', '', '100'])
        canonic_table.append(['car', 'gas', '5'])
        canonic_table.append(['car', 'gas', '-20'])
        canonic_table.append(['', '', '20'])
        canonic_table.append(['', '', '10'])

        query = r'select b1,b2,   a1 left join {} on a2 == b1 where b2 != "wings"'.format(join_table_path)
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, ['null_value_in_output'], warnings)

        if TEST_JS:
            query = r'select b1,b2,   a1 left join {} on a2 == b1 where b2 != "wings"'.format(join_table_path)
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, ['null_value_in_output'], warnings)


    def test_run8(self):
        test_name = 'test8'

        input_table = list()
        input_table.append(['5', 'car', 'lada'])
        input_table.append(['-20', 'car', 'ferrari'])
        input_table.append(['50', 'plane', 'tu-134'])
        input_table.append(['20', 'boat', 'destroyer'])
        input_table.append(['10', 'boat', 'yacht'])
        input_table.append(['200', 'plane', 'boeing 737'])
        input_table.append(['100', 'magic carpet', 'nimbus 3000'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        join_table = list()
        join_table.append(['bicycle', 'legs'])
        join_table.append(['car', 'gas'])
        join_table.append(['plane', 'wings'])
        join_table.append(['rocket', 'some stuff'])

        join_table_path = os.path.join(tempfile.gettempdir(), '{}_rhs_join_table.tsv'.format(test_name))
        table_to_file(join_table, join_table_path, input_delim, input_policy)

        query = r'select b1,b2,   a1 strict left join {} on a2 == b1 where b2 != "wings"'.format(join_table_path)
        with self.assertRaises(Exception) as cm:
            test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        e = cm.exception
        self.assertTrue(str(e).find('In "STRICT LEFT JOIN" each key in A must have exactly one match in B') != -1)

        if TEST_JS:
            query = r'select b1,b2,   a1 strict left join {} on a2 == b1 where b2 != "wings"'.format(join_table_path)
            with self.assertRaises(Exception) as cm:
                test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            e = cm.exception
            self.assertTrue(str(e).find('In "STRICT LEFT JOIN" each key in A must have exactly one match in B') != -1)


    def test_run9(self):
        test_name = 'test9'

        input_table = list()
        input_table.append(['5', 'car', 'lada'])
        input_table.append(['-20', 'car', 'ferrari'])
        input_table.append(['50', 'plane', 'tu-134'])
        input_table.append(['200', 'plane', 'boeing 737'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        join_table = list()
        join_table.append(['bicycle', 'legs'])
        join_table.append(['car', 'gas'])
        join_table.append(['plane', 'wings'])
        join_table.append(['plane', 'air'])
        join_table.append(['rocket', 'some stuff'])

        join_table_path = os.path.join(tempfile.gettempdir(), '{}_rhs_join_table.tsv'.format(test_name))
        table_to_file(join_table, join_table_path, input_delim, input_policy)

        canonic_table = list()
        canonic_table.append(['plane', 'wings', '50'])
        canonic_table.append(['plane', 'air', '50'])
        canonic_table.append(['plane', 'wings', '200'])
        canonic_table.append(['plane', 'air', '200'])

        query = r'select b1,b2,a1 inner join {} on a2 == b1 where b1 != "car"'.format(join_table_path)
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'select b1,b2,a1 inner join {} on a2 == b1 where b1 != "car"'.format(join_table_path)
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run10(self):
        test_name = 'test10'

        input_table = list()
        input_table.append(['5', 'haha', 'hoho'])
        input_table.append(['-20', 'haha', 'hioho'])
        input_table.append(['50', 'haha', 'dfdf'])
        input_table.append(['20', 'haha', ''])

        canonic_table = list()
        canonic_table.append(['5', 'haha', 'hoho'])
        canonic_table.append(['50', 'haha', 'dfdf'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = 'select * where a3 =="hoho" or int(a1)==50 or a1 == "aaaa" or a2== "bbbbb" '
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = 'select * where a3 =="hoho" || parseInt(a1)==50 || a1 == "aaaa" || a2== "bbbbb" '
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run11(self):
        test_name = 'test11'

        input_table = list()
        input_table.append(['5', 'Петр Первый', 'hoho'])
        input_table.append(['-20', 'Екатерина Великая', 'hioho'])
        input_table.append(['50', 'Наполеон', 'dfdf'])
        input_table.append(['20', 'Наполеон', ''])

        canonic_table = list()
        canonic_table.append(['50', 'Наполеон', 'dfdf'])
        canonic_table.append(['20', 'Наполеон', ''])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = 'select * where a2== "Наполеон" '
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy, join_csv_encoding='utf-8')
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = 'select * where a2== "Наполеон" '
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy, csv_encoding='utf-8')
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run12(self):
        test_name = 'test12'

        input_table = list()
        input_table.append(['5', 'car', 'lada'])
        input_table.append(['-20', 'car', 'Ferrari'])
        input_table.append(['50', 'plane', 'tu-134'])
        input_table.append(['20', 'boat', 'destroyer'])
        input_table.append(['10', 'boat', 'yacht'])
        input_table.append(['200', 'plane', 'boeing 737'])
        input_table.append(['80', 'train', 'Thomas'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        join_table = list()
        join_table.append(['bicycle', 'legs'])
        join_table.append(['car', 'gas'])
        join_table.append(['plane', 'wings'])
        join_table.append(['boat', 'wind'])
        join_table.append(['rocket', 'some stuff'])

        join_table_path = os.path.join(tempfile.gettempdir(), '{}_rhs_join_table.tsv'.format(test_name))
        table_to_file(join_table, join_table_path, input_delim, input_policy)

        canonic_table = list()
        canonic_table.append(['5', '10', 'boat', 'yacht', 'boat', 'wind'])
        canonic_table.append(['4', '20', 'boat', 'destroyer', 'boat', 'wind'])
        canonic_table.append(['2', '-20', 'car', 'Ferrari', 'car', 'gas'])
        canonic_table.append(['1', '5', 'car', 'lada', 'car', 'gas'])
        canonic_table.append(['3', '50', 'plane', 'tu-134', 'plane', 'wings'])
        canonic_table.append(['6', '200', 'plane', 'boeing 737', 'plane', 'wings'])

        query = r'select NR, * JOIN {} on a2 == b1 where b2 != "haha" and int(a1) > -100 and len(b2) > 1 order   by a2, int(a1)'.format(join_table_path)
        test_table, warnings= run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'select NR, * JOIN {} on a2 == b1 where b2 != "haha" && a1 > -100 && b2.length > 1 order    by a2, parseInt(a1)'.format(join_table_path)
            test_table, warnings= run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run13(self):
        test_name = 'test13'

        input_table = list()
        input_table.append(['5', 'haha   asdf', 'hoho'])
        input_table.append(['50', 'haha  asdf', 'dfdf'])
        input_table.append(['20', 'haha    asdf', ''])
        input_table.append(['-20', 'haha   asdf', 'hioho'])

        canonic_table = list()
        canonic_table.append(['5', 'haha   asdf', 'hoho'])
        canonic_table.append(['-20', 'haha   asdf', 'hioho'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = r'select * where re.search("a   as", a2)  is   not  None'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'select * where /a   as/.test(a2)'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run14(self):
        test_name = 'test14'

        input_table = list()
        input_table.append(['5', 'haha   asdf', 'hoho'])
        input_table.append(['50', 'haha  asdf', 'dfdf'])
        input_table.append(['20', 'haha    asdf', ''])
        input_table.append(['-20', 'haha   asdf', 'hioho'])

        canonic_table = list()
        canonic_table.append(['5', 'haha   asdf', 'hoho'])
        canonic_table.append(['100', 'haha  asdf hoho', 'dfdf'])
        canonic_table.append(['100', 'haha    asdf hoho', ''])
        canonic_table.append(['-20', 'haha   asdf', 'hioho'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = r'update a2 = a2 + " hoho", a1 = 100 where int(a1) > 10'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'update a2 = a2 + " hoho", a1 = 100 where parseInt(a1) > 10'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run15(self):
        test_name = 'test15'

        input_table = list()
        input_table.append(['5', 'Петр Первый', 'hoho'])
        input_table.append(['-20', 'Екатерина Великая', 'hioho'])
        input_table.append(['50', 'Наполеон', 'dfdf'])
        input_table.append(['20', 'Наполеон'])

        canonic_table = list()
        canonic_table.append(['5', 'Наполеон', 'hoho'])
        canonic_table.append(['-20', 'Наполеон', 'hioho'])
        canonic_table.append(['50', 'Наполеон', 'dfdf'])
        canonic_table.append(['20', 'Наполеон'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = 'update set a2= "Наполеон" '
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy, join_csv_encoding='utf-8')
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, ['input_fields_info'], warnings)

        if TEST_JS:
            query = 'update  set  a2= "Наполеон" '
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy, csv_encoding='utf-8')
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, ['input_fields_info'], warnings)


    def test_run16(self):
        test_name = 'test16'

        input_table = list()
        input_table.append(['100', 'magic carpet', 'nimbus 3000'])
        input_table.append(['5', 'car', 'lada'])
        input_table.append(['-20', 'car', 'ferrari'])
        input_table.append(['50', 'plane', 'tu-134'])
        input_table.append(['20', 'boat', 'destroyer'])
        input_table.append(['10', 'boat', 'yacht'])
        input_table.append(['200', 'plane', 'boeing 737'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        join_table = list()
        join_table.append(['bicycle', 'legs'])
        join_table.append(['car', 'gas'])
        join_table.append(['plane', 'wings'])
        join_table.append(['rocket', 'some stuff'])

        join_table_path = os.path.join(tempfile.gettempdir(), '{}_rhs_join_table.tsv'.format(test_name))
        table_to_file(join_table, join_table_path, input_delim, input_policy)

        canonic_table = list()
        canonic_table.append(['100', 'magic carpet', 'nimbus 3000'])
        canonic_table.append(['5', 'car (gas)', 'lada'])
        canonic_table.append(['-20', 'car (gas)', 'ferrari'])
        canonic_table.append(['50', 'plane', 'tu-134'])
        canonic_table.append(['20', 'boat', 'destroyer'])
        canonic_table.append(['10', 'boat', 'yacht'])
        canonic_table.append(['200', 'plane', 'boeing 737'])

        query = r'update set a2 = "{} ({})".format(a2, b2) inner join ' + join_table_path + ' on a2 == b1 where b2 != "wings"'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'update set a2 = a2 + " (" + b2 + ")" inner join ' + join_table_path + ' on a2 == b1 where b2 != "wings"'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run17(self):
        test_name = 'test17'

        input_table = list()
        input_table.append(['cde', '1234'])
        input_table.append(['abc', '1234'])
        input_table.append(['abc', '1234'])
        input_table.append(['efg', '100'])
        input_table.append(['abc', '100'])
        input_table.append(['cde', '12999'])
        input_table.append(['aaa', '2000'])
        input_table.append(['abc', '100'])

        canonic_table = list()
        canonic_table.append(['2', 'cde'])
        canonic_table.append(['4', 'abc'])
        canonic_table.append(['1', 'efg'])
        canonic_table.append(['1', 'aaa'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = r'select distinct count a1 where int(a2) > 10'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'select distinct count a1 where parseInt(a2) > 10'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run18(self):
        test_name = 'test18'

        input_table = list()
        input_table.append(['\xef\xbb\xbfcde', '1234'])
        input_table.append(['abc', '1234'])
        input_table.append(['abc', '1234'])
        input_table.append(['efg', '100'])
        input_table.append(['abc', '100'])
        input_table.append(['cde', '12999'])
        input_table.append(['aaa', '2000'])
        input_table.append(['abc', '100'])

        canonic_table = list()
        canonic_table.append(['1', 'efg'])
        canonic_table.append(['4', 'abc'])

        input_delim, input_policy, output_delim, output_policy = ['\t', 'simple', '\t', 'simple']

        query = r'select top 2 distinct count a1 where int(a2) > 10 order by int(a2) asc'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, ['utf8_bom_removed'], warnings)

        if TEST_JS:
            query = r'select top 2 distinct count a1 where parseInt(a2) > 10 order by parseInt(a2) asc'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, ['utf8_bom_removed'], warnings)


    def test_run18b(self):
        test_name = 'test18b'

        input_table = list()
        input_table.append(['cde', '1234'])
        input_table.append(['abc', '1234'])
        input_table.append(['abc', '1234'])
        input_table.append(['efg', '100'])
        input_table.append(['abc', '100'])
        input_table.append(['cde', '12999'])
        input_table.append(['aaa', '2000'])
        input_table.append(['abc', '100'])

        canonic_table = list()
        canonic_table.append(['1', 'efg'])
        canonic_table.append(['4', 'abc'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = r'select distinct count a1 where int(a2) > 10 order by int(a2) asc limit   2  '
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'select distinct count a1 where parseInt(a2) > 10 order by parseInt(a2) asc limit 2'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run19(self):
        test_name = 'test19'

        input_table = list()
        input_table.append(['5', 'car', 'lada'])
        input_table.append(['-20', 'car', 'ferrari'])
        input_table.append(['50', 'plane', 'tu-134'])
        input_table.append(['200', 'plane', 'boeing 737'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        join_table = list()
        join_table.append(['bicycle', 'legs'])
        join_table.append(['car', 'gas'])
        join_table.append(['plane', 'wings'])
        join_table.append(['rocket', 'some stuff'])

        join_table_path = os.path.join(tempfile.gettempdir(), '{}_rhs_join_table.tsv'.format(test_name))
        table_to_file(join_table, join_table_path, input_delim, input_policy)

        canonic_table = list()
        canonic_table.append(['3', 'car'])
        canonic_table.append(['3', 'car'])
        canonic_table.append(['5', 'plane'])
        canonic_table.append(['5', 'plane'])

        query = r'select len(b1), a2 strict left join {} on a2 == b1'.format(join_table_path)
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'select b1.length,  a2 strict left join {} on a2 == b1'.format(join_table_path)
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run20(self):
        test_name = 'test20'

        input_table = list()
        input_table.append(['100', 'magic carpet', 'nimbus 3000'])
        input_table.append(['5', 'car', 'lada'])
        input_table.append(['-20', 'car', 'ferrari'])
        input_table.append(['50', 'plane', 'tu-134'])
        input_table.append(['20', 'boat', 'destroyer'])
        input_table.append(['10', 'boat', 'yacht'])
        input_table.append(['200', 'plane', 'boeing 737'])

        input_delim, input_policy, output_delim, output_policy = ['\t', 'simple', '\t', 'simple']

        join_table = list()
        join_table.append(['\xef\xbb\xbfbicycle', 'legs'])
        join_table.append(['car', 'gas'])
        join_table.append(['plane', 'wings'])
        join_table.append(['rocket', 'some stuff'])

        join_table_path = os.path.join(tempfile.gettempdir(), '{}_rhs_join_table.tsv'.format(test_name))
        table_to_file(join_table, join_table_path, input_delim, input_policy)

        canonic_table = list()
        canonic_table.append(['100', 'magic carpet', ''])
        canonic_table.append(['5', 'car', 'gas'])
        canonic_table.append(['-20', 'car', 'gas'])
        canonic_table.append(['50', 'plane', 'tu-134'])
        canonic_table.append(['20', 'boat', ''])
        canonic_table.append(['10', 'boat', ''])
        canonic_table.append(['200', 'plane', 'boeing 737'])

        query = r'update set a3 = b2 left join ' + join_table_path + ' on a2 == b1 where b2 != "wings"'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, ['utf8_bom_removed', 'null_value_in_output'], warnings)

        if TEST_JS:
            query = r'update set a3 = b2 left join ' + join_table_path + ' on a2 == b1 where b2 != "wings"'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, ['utf8_bom_removed', 'null_value_in_output'], warnings)


    def test_run21(self):
        test_name = 'test21'

        input_table = list()
        input_table.append(['cde'])
        input_table.append(['abc'])
        input_table.append(['abc'])
        input_table.append(['efg'])
        input_table.append(['abc'])
        input_table.append(['cde'])
        input_table.append(['aaa'])
        input_table.append(['abc'])

        canonic_table = list()
        canonic_table.append(['cde'])
        canonic_table.append(['abc'])
        canonic_table.append(['efg'])
        canonic_table.append(['aaa'])

        input_delim = ''
        input_policy = 'monocolumn'
        output_delim = '\t'
        output_policy = 'simple'

        query = r'select distinct a1'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'select distinct a1'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run22(self):
        test_name = 'test22'

        input_table = list()
        input_table.append(['100', 'magic carpet', 'nimbus 3000'])
        input_table.append(['5', 'car', 'lada'])
        input_table.append(['-20', 'car', 'ferrari'])
        input_table.append(['50', 'plane', 'tu-134'])
        input_table.append(['20', 'boat', 'destroyer'])
        input_table.append(['10', 'boat', 'yacht'])
        input_table.append(['200', 'plane', 'boeing 737'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        join_table = list()
        join_table.append(['bicycle'])
        join_table.append(['car'])
        join_table.append(['plane'])
        join_table.append(['rocket'])

        join_delim = ''
        join_policy = 'monocolumn'
        join_table_path = os.path.join(tempfile.gettempdir(), '{}_rhs_join_table.tsv'.format(test_name))
        table_to_file(join_table, join_table_path, join_delim, join_policy)
        update_index(rbql.table_index_path, [join_table_path, join_delim, join_policy, ''], 100)

        canonic_table = list()
        canonic_table.append(['5', 'car', 'lada'])
        canonic_table.append(['-20', 'car', 'ferrari'])
        canonic_table.append(['50', 'plane', 'tu-134'])
        canonic_table.append(['200', 'plane', 'boeing 737'])

        query = r'select a1,a2,a3 left join ' + join_table_path + ' on a2 == b1 where b1 is not None'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'select a1,a2,a3 left join ' + join_table_path + ' on a2 == b1 where b1 != null'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run23(self):
        test_name = 'test23'

        input_table = list()
        input_table.append(['car', '1', '100', '1'])
        input_table.append(['car', '2', '100', '1'])
        input_table.append(['dog', '3', '100', '2'])
        input_table.append(['car', '4', '100', '2'])
        input_table.append(['cat', '5', '100', '3'])
        input_table.append(['cat', '6', '100', '3'])
        input_table.append(['car', '7', '100', '100'])
        input_table.append(['car', '8', '100', '100'])

        canonic_table = list()
        canonic_table.append(['100', '10', '8', '8', '8', '8', '800', '4.5', '5.25', '2.5'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = r'select a3, MIN(int(a2) * 10), MAX(a2), COUNT(*), COUNT(1), COUNT(a1), SUM(a3), AVG(a2), VARIANCE(a2), MEDIAN(a4)'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'select a3, MIN(a2 * 10), MAX(a2), COUNT(*), COUNT(1), COUNT(a1), SUM(a3), AVG(a2), VARIANCE(a2), MEDIAN(a4)'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run24(self):
        test_name = 'test24'

        input_table = list()
        input_table.append(['car', '1', '100', '1'])
        input_table.append(['car', '2', '100', '1'])
        input_table.append(['dog', '3', '100', '2'])
        input_table.append(['car', '4', '100', '2'])
        input_table.append(['cat', '5', '100', '3'])
        input_table.append(['cat', '6', '100', '3'])
        input_table.append(['car', '7', '100', '100'])
        input_table.append(['car', '8', '100', '100'])

        canonic_table = list()
        canonic_table.append(['car', '100', '10', '8', '5', '5', '5', '500', '4.4', '7.44', '2'])
        canonic_table.append(['cat', '100', '50', '6', '2', '2', '2', '200', '5.5', '0.25', '3'])
        canonic_table.append(['dog', '100', '30', '3', '1', '1', '1', '100', '3.0', '0.0', '2'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = r'select a1, a3, MIN(int(a2) * 10), MAX(a2), COUNT(*), COUNT(1), COUNT(a1), SUM(a3), AVG(a2), VARIANCE(a2), MEDIAN(a4) group by a1'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'select a1, a3, MIN(a2 * 10), MAX(a2), COUNT(*), COUNT(1), COUNT(a1), SUM(a3), AVG(a2), VARIANCE(a2), MEDIAN(a4) group by a1'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run25(self):
        test_name = 'test25'

        input_table = list()
        input_table.append(['car', '1', '100', '1'])
        input_table.append(['car', '2', '100', '1'])
        input_table.append(['dog', '3', '100', '2'])
        input_table.append(['car', '4', '100', '2'])
        input_table.append(['cat', '5', '100', '3'])
        input_table.append(['cat', '6', '100', '3'])
        input_table.append(['car', '7', '100', '100'])
        input_table.append(['car', '8', '100', '100'])

        canonic_table = list()
        canonic_table.append(['car', '100', '10', '8', '5', '5', '5', '500', '4.4', '7.44', '2'])
        canonic_table.append(['dog', '100', '30', '3', '1', '1', '1', '100', '3.0', '0.0', '2'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = r'select a1, a3, MIN(int(a2) * 10), MAX(a2), COUNT(*), COUNT(1), COUNT(a1), SUM(a3), AVG(a2), VARIANCE(a2), MEDIAN(a4) where a1 != "cat" group by a1'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'select a1, a3, MIN(a2 * 10), MAX(a2), COUNT(*), COUNT(1), COUNT(a1), SUM(a3), AVG(a2), VARIANCE(a2), MEDIAN(a4) where a1 != "cat" group by a1'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run26(self):
        test_name = 'test26'

        input_table = list()
        input_table.append(['5', 'haha', 'hoho'])
        input_table.append(['-20', 'haha', 'hioho'])
        input_table.append(['50', 'haha', 'dfdf'])
        input_table.append(['20', 'haha', ''])

        canonic_table = list()
        canonic_table.append(['haha;', '5'])
        canonic_table.append(['haha', '', '-20'])
        canonic_table.append(['haha;', '50'])
        canonic_table.append(['haha', '', '20'])

        input_delim = ','
        input_policy = 'simple'
        output_delim = ','
        output_policy = 'simple'

        query = 'select a2 + "," if NR % 2 == 0 else a2 + ";", a1'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, ['delim_in_simple_output'], warnings)

        if TEST_JS:
            query = 'select NR % 2 == 0 ? a2 + "," : a2 + ";", a1'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, ['delim_in_simple_output'], warnings)


    def test_run27(self):
        test_name = 'test27'

        input_table = list()
        input_table.append(['5', 'haha   asdf', 'hoho'])
        input_table.append(['50', 'haha  asdf', 'dfdf'])
        input_table.append(['20', 'haha    asdf', ''])
        input_table.append(['-20', 'haha   asdf', 'hioho'])
        input_table.append(['40', 'lol', 'hioho'])

        canonic_table = list()
        canonic_table.append(['5', 'haha   asdf', 'hoho'])
        canonic_table.append(['100', 'haha  asdf 1', 'dfdf'])
        canonic_table.append(['100', 'haha    asdf 2', ''])
        canonic_table.append(['-20', 'haha   asdf', 'hioho'])
        canonic_table.append(['100', 'lol 3', 'hioho'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = r'update a2 = "{} {}".format(a2, NU) , a1 = 100 where int(a1) > 10'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'update a2 = a2 + " " + NU, a1 = 100 where parseInt(a1) > 10'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run28(self):
        test_name = 'test28'

        input_table = list()
        input_table.append(['cde'])
        input_table.append(['abc'])
        input_table.append(['a,bc'])
        input_table.append(['efg'])

        canonic_table = list()
        canonic_table.append(['cde,cde2'])
        canonic_table.append(['abc,abc2'])
        canonic_table.append(['"a,bc","a,bc2"'])
        canonic_table.append(['efg,efg2'])

        input_delim = ''
        input_policy = 'monocolumn'
        output_delim = ''
        output_policy = 'monocolumn'

        query = r'select a1, a1 + "2"'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, ['output_switch_to_csv'], warnings)

        if TEST_JS:
            query = r'select a1, a1 + "2"'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, ['output_switch_to_csv'], warnings)


    def test_run29(self):
        test_name = 'test29'
        if not TEST_JS:
            # JS inerpolation test
            return

        input_table = list()
        input_table.append(['cde', 'hello'])
        input_table.append(['abc', 'world'])
        input_table.append(['abc', 'stack'])

        canonic_table = list()
        canonic_table.append(['mv cde hello1 --opt1 --opt2'])
        canonic_table.append(['mv abc world2 --opt1 --opt2'])
        canonic_table.append(['mv abc stack3 --opt1 --opt2'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = r'select `mv ${a1} ${a2 + NR} --opt1 --opt2`'
        test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)


    def test_run30(self):
        test_name = 'test30'

        input_table = list()
        input_table.append(['5', 'haha', 'hoho'])
        input_table.append(['-20', 'haha', 'hioho'])
        input_table.append(['50', 'haha', 'dfdf'])
        input_table.append(['20', 'haha', ''])

        canonic_table = list()
        canonic_table.append(['2'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = 'select NR where a3 == "hioho"'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = 'select NR where a3 == "hioho"'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run31(self):
        test_name = 'test31'

        input_table = list()
        input_table.append(['5', 'haha', 'hoho'])
        input_table.append(['-20', 'haha', 'hioho'])
        input_table.append(['50', 'haha', 'dfdf'])
        input_table.append(['20', 'haha', ''])

        canonic_table = list()
        canonic_table.append(['2'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = 'select NR where a3 = "hioho"'
        with self.assertRaises(Exception) as cm:
            test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        e = cm.exception
        self.assertTrue(str(e).find('Assignments "=" are not allowed in "WHERE" expressions. For equality test use "=="') != -1)

        if TEST_JS:
            query = 'select NR where a3 = "hioho"'
            with self.assertRaises(Exception) as cm:
                test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            e = cm.exception
            self.assertTrue(str(e).find('Assignments "=" are not allowed in "WHERE" expressions. For equality test use "==" or "==="') != -1)


    def test_run32(self):
        test_name = 'test32'

        input_table = list()
        input_table.append(['5', 'haha', 'hoho'])
        input_table.append(['-20', 'haha', 'hioho'])
        input_table.append(['50', 'haha', 'dfdf'])
        input_table.append(['20', 'haha', ''])

        canonic_table = list()
        canonic_table.append(['2'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        if TEST_JS:
            query = 'select NR where a3 === "hioho"'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run33(self):
        test_name = 'test33'
        input_table = list()
        input_table.append(['5', 'haha', 'hoho'])
        input_table.append(['-20', 'haha', 'hioho'])
        input_table.append(['50', 'haha', 'dfdf'])
        input_table.append(['20', 'haha', ''])

        canonic_table = list()
        canonic_table.append(['5', 'haha FOObar', 'hoho'])
        canonic_table.append(['-20', 'haha FOObar', 'hioho'])
        canonic_table.append(['50', 'haha FOObar', 'dfdf'])
        canonic_table.append(['20', 'haha FOObar', ''])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = r'select a1, foobar(a2), a3'
        with tempfile.NamedTemporaryFile() as init_tmp_file:
            with open(init_tmp_file.name, 'w') as tf:
                tf.write('def foobar(val):\n    return val + " FOObar"\r\n\n')
            test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy, custom_init_path=init_tmp_file.name)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)

        if TEST_JS:
            with tempfile.NamedTemporaryFile() as init_tmp_file:
                with open(init_tmp_file.name, 'w') as tf:
                    tf.write('function foobar(val) {\n    return val + " FOObar";\r\n}\n')
                query = r'select a1, foobar(a2), a3'
                test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy, custom_init_path=init_tmp_file.name)
                self.compare_tables(canonic_table, test_table)
                compare_warnings(self, None, warnings)


    def test_run34(self):
        test_name = 'test34'

        input_table = list()
        input_table.append(['5', 'haha', 'hoho'])
        input_table.append(['-20', 'haha', 'hioho'])
        input_table.append(['50', 'haha', 'dfdf'])
        input_table.append(['20', 'haha', 'mmmmm'])

        canonic_table = list()
        canonic_table.append(['3', '50', '4'])
        canonic_table.append(['4', '20', '5'])

        input_delim = ' '
        input_policy = 'whitespace'
        output_delim = '\t'
        output_policy = 'simple'

        query = 'select NR, a1, len(a3) where int(a1) > 5'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = 'select NR, a1, a3.length where a1 > 5'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run35(self):
        test_name = 'test35'

        input_table = list()
        input_table.append(['car', '1', '100', '1'])
        input_table.append(['car', '2', '100', '1'])
        input_table.append(['dog', '3', '100', '2'])
        input_table.append(['car', '4', '100', '2'])
        input_table.append(['cat', '5', '100', '3'])
        input_table.append(['cat', '6', '100', '3'])
        input_table.append(['car', '7', '100', '100'])
        input_table.append(['car', '8', '100', '100'])

        canonic_table = list()
        canonic_table.append(['1|2|4|7|8', 'car', '5'])
        canonic_table.append(['3', 'dog', '1'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table, '\t,;:')

        query = r'select FOLD(a2), a1, FOLD(a4, lambda v: len(v)) where a1 == "car" or a1 == "dog" group by a1'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'select FOLD(a2), a1, FOLD(a4, v => v.length) where a1 == "car" || a1 == "dog" group by a1'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run36(self):
        test_name = 'test36'

        input_table = list()
        input_table.append(['car', '1', '100', '1'])
        input_table.append(['car', '2', '100', '1'])
        input_table.append(['dog', '3', '100', '2'])

        canonic_table = list()
        canonic_table.append(['1', 'car', '100', '1'])
        canonic_table.append(['2', 'car', '100', '1'])
        canonic_table.append(['3', 'dog', '100', '2'])

        input_delim, input_policy, output_delim, output_policy = select_random_formats(input_table)

        query = r'update set a1 = a2, a2 = a1'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'update set a1 = a2, a2 = a1'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run37(self):
        test_name = 'test37'

        input_table = list()
        input_table.append(['car', '1'])
        input_table.append(['car', '2'])
        input_table.append(['car', '4'])
        input_table.append(['dog', '3'])

        canonic_table = list()
        canonic_table.append(['car', '1|2|4'])
        canonic_table.append(['dog', '3'])

        input_delim, input_policy, output_delim, output_policy = ['\t', 'simple', '\t', 'simple']

        # Step 1: FOLD
        query = r'select a1, FOLD(a2) group by a1'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        # Step 2: UNFOLD back to original
        query = r'select a1, UNFOLD(a2.split("|"))'
        test_table, warnings = run_conversion_test_py(query, canonic_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(input_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            # Step 1: FOLD
            query = r'select a1, FOLD(a2) group by a1'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)

            # Step 2: UNFOLD back to original
            query = r'select a1, UNFOLD(a2.split("|"))'
            test_table, warnings = run_conversion_test_js(query, canonic_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(input_table, test_table)
            compare_warnings(self, None, warnings)


    def test_run38(self):
        test_name = 'test38'

        input_table = list()
        input_table.append(['car', '1', '100', '1'])
        input_table.append(['car', '2', '100', '1'])
        input_table.append(['dog', '3', '100', '2'])
        input_table.append(['mouse', '2', '100', '1'])

        canonic_table = list()
        canonic_table.append(['car|car|dog|mouse'])

        input_delim, input_policy, output_delim, output_policy =  select_random_formats(input_table, '\t,;:')

        query = r'select FOLD(a1)'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'select FOLD(a1)'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)

    def test_run39(self):
        test_name = 'test39'

        input_table = list()
        input_table.append(['car', '1', '100', '1'])
        input_table.append(['car', '2', '100', '1'])
        input_table.append(['dog', '3', '100', '2'])
        input_table.append(['mouse', '2', '50', '1'])

        canonic_table = list()
        canonic_table.append(['mouse', '50'])
        canonic_table.append(['dog', '100'])
        canonic_table.append(['car', '100'])

        input_delim, input_policy, output_delim, output_policy =  select_random_formats(input_table)

        query = r'select top 3 * except a2, a4 order by a1 desc'
        test_table, warnings = run_conversion_test_py(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
        self.compare_tables(canonic_table, test_table)
        compare_warnings(self, None, warnings)

        if TEST_JS:
            query = r'select top 3 * except a2, a4 order by a1 desc'
            test_table, warnings = run_conversion_test_js(query, input_table, test_name, input_delim, input_policy, output_delim, output_policy)
            self.compare_tables(canonic_table, test_table)
            compare_warnings(self, None, warnings)


def calc_file_md5(fname):
    import hashlib
    hash_md5 = hashlib.md5()
    with open(fname, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


class TestFiles(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.old_dir = os.getcwd()
        ut_dir = os.path.join(script_dir, 'unit_tests')
        os.chdir(ut_dir)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.old_dir)

    def test_all(self):
        import json
        ut_config_path = 'unit_tests.cfg'
        with codecs.open(ut_config_path, encoding='utf-8') as src:
            for test_no, line in enumerate(src, 1):
                config = json.loads(line)
                backend_language = config.get('backend_language', 'python')
                if not TEST_JS and backend_language == 'js':
                    continue
                src_path = config['src_table']
                canonic_table = config.get('canonic_table')
                canonic_error_msg = config.get('canonic_error_msg')
                canonic_warnings = config.get('warnings')
                if canonic_warnings is not None:
                    canonic_warnings = canonic_warnings.split(',')
                query = config['query']
                encoding = config.get('encoding', default_csv_encoding)
                delim = config.get('delim', 'TAB')
                if delim == 'TAB':
                    delim = '\t'
                default_policy = 'quoted' if delim in [';', ','] else 'simple'
                policy = config.get('policy', default_policy)
                out_format = config.get('out_format')
                canonic_path = None if canonic_table is None else os.path.abspath(canonic_table)
                canonic_md5 = calc_file_md5(canonic_table)

                if backend_language == 'python':
                    warnings = None
                    try:
                        result_table, warnings = run_file_query_test_py(query, src_path, str(test_no), delim, policy, encoding)
                    except Exception as e:
                        if canonic_error_msg is None or str(e).find(canonic_error_msg) == -1:
                            raise
                        continue
                    test_path = os.path.abspath(result_table) 
                    test_md5 = calc_file_md5(result_table)
                    self.assertEqual(test_md5, canonic_md5, msg='Tables missmatch. Canonic: {} Actual: {}'.format(canonic_path, test_path))
                    compare_warnings(self, canonic_warnings, warnings)
                else: 
                    assert backend_language == 'js'
                    try:
                        result_table, warnings = run_file_query_test_js(query, src_path, str(test_no), delim, policy, encoding, out_format)
                    except Exception as e:
                        if canonic_error_msg is None or str(e).find(canonic_error_msg) == -1:
                            raise
                        continue
                    test_path = os.path.abspath(result_table) 
                    test_md5 = calc_file_md5(result_table)
                    self.assertEqual(test_md5, canonic_md5, msg='Tables missmatch. Canonic: {} Actual: {}'.format(canonic_path, test_path))
                    compare_warnings(self, canonic_warnings, warnings)



class TestStringMethods(unittest.TestCase):
    def test_strip4(self):
        a = ''' # a comment  '''
        a_strp = rbql.strip_py_comments(a)
        self.assertEqual(a_strp, '')


def natural_random(low, high):
    if low <= 0 and high >= 0 and random.randint(0, 2) == 0:
        return 0
    k = random.randint(0, 8)
    if k < 2:
        return low + k
    if k > 6:
        return high - 8 + k
    return random.randint(low, high)


def make_random_csv_fields(num_fields, max_field_len):
    available = [',', '"', 'a', 'b', 'c', 'd']
    result = list()
    for fn in range(num_fields):
        flen = natural_random(0, max_field_len)
        chosen = list()
        for i in range(flen):
            chosen.append(random.choice(available))
        result.append(''.join(chosen))
    return result


def randomly_csv_escape(fields):
    efields = list()
    for field in fields:
        efields.append(stochastic_quote_field(field, ','))
    assert unquote_fields(efields) == fields
    return ','.join(efields)


def make_random_csv_records():
    result = list()
    for num_test in rbql.xrange6(1000):
        num_fields = random.randint(1, 11)
        max_field_len = 25
        fields = make_random_csv_fields(num_fields, max_field_len)
        csv_line = randomly_csv_escape(fields)
        defective_escaping = random.randint(0, 1)
        if defective_escaping:
            defect_pos = random.randint(0, len(csv_line))
            csv_line = csv_line[:defect_pos] + '"' + csv_line[defect_pos:]
        result.append((fields, csv_line, defective_escaping))
    return result


class TestSplitMethods(unittest.TestCase):

    def test_split(self):
        test_cases = list()
        test_cases.append(('hello,world', (['hello','world'], False)))
        test_cases.append(('hello,"world"', (['hello','world'], False)))
        test_cases.append(('"abc"', (['abc'], False)))
        test_cases.append(('abc', (['abc'], False)))
        test_cases.append(('', ([''], False)))
        test_cases.append((',', (['',''], False)))
        test_cases.append((',,,', (['','','',''], False)))
        test_cases.append((',"",,,', (['','','','',''], False)))
        test_cases.append(('"","",,,""', (['','','','',''], False)))
        test_cases.append(('"aaa,bbb",', (['aaa,bbb',''], False)))
        test_cases.append(('"aaa,bbb",ccc', (['aaa,bbb','ccc'], False)))
        test_cases.append(('"aaa,bbb","ccc"', (['aaa,bbb','ccc'], False)))
        test_cases.append(('"aaa,bbb","ccc,ddd"', (['aaa,bbb','ccc,ddd'], False)))
        test_cases.append((' "aaa,bbb" ,  "ccc,ddd" ', (['aaa,bbb','ccc,ddd'], False)))
        test_cases.append(('"aaa,bbb",ccc,ddd', (['aaa,bbb','ccc', 'ddd'], False)))
        test_cases.append(('"a"aa" a,bbb",ccc,ddd', (['"a"aa" a', 'bbb"','ccc', 'ddd'], True)))
        test_cases.append(('"aa, bb, cc",ccc",ddd', (['aa, bb, cc','ccc"', 'ddd'], True)))
        test_cases.append(('hello,world,"', (['hello','world', '"'], True)))
        for tc in test_cases:
            src = tc[0]
            canonic_dst = tc[1]
            warning_expected = canonic_dst[1]
            test_dst = rbql_utils.split_quoted_str(tc[0], ',')
            self.assertEqual(canonic_dst, test_dst, msg = '\nsrc: {}\ntest_dst: {}\ncanonic_dst: {}\n'.format(src, test_dst, canonic_dst))

            test_dst_preserved = rbql_utils.split_quoted_str(tc[0], ',', True)
            self.assertEqual(test_dst[1], test_dst_preserved[1])
            self.assertEqual(','.join(test_dst_preserved[0]), tc[0], 'preserved split failure')
            if not warning_expected:
                self.assertEqual(test_dst[0], unquote_fields(test_dst_preserved[0]))


    def test_unquote(self):
        test_cases = list()
        test_cases.append(('  "hello, ""world"" aa""  " ', 'hello, "world" aa"  '))
        for tc in test_cases:
            src, canonic = tc
            test_dst = unquote_field(src)
            self.assertEqual(canonic, test_dst)


    def test_split_whitespaces(self):
        test_cases = list()
        test_cases.append(('hello world', (['hello','world'], False)))
        test_cases.append(('hello   world', (['hello','world'], False)))
        test_cases.append(('   hello   world   ', (['hello','world'], False)))
        test_cases.append(('     ', ([], False)))
        test_cases.append(('', ([], False)))
        test_cases.append(('   a   b  c d ', (['a', 'b', 'c', 'd'], False)))

        test_cases.append(('hello world', (['hello ','world'], True)))
        test_cases.append(('hello   world', (['hello   ','world'], True)))
        test_cases.append(('   hello   world   ', (['   hello   ','world   '], True)))
        test_cases.append(('     ', ([], True)))
        test_cases.append(('', ([], True)))
        test_cases.append(('   a   b  c d ', (['   a   ', 'b  ', 'c ', 'd '], True)))

        for tc in test_cases:
            src = tc[0]
            canonic_dst, preserve_whitespaces = tc[1]
            test_dst = rbql_utils.split_whitespace_separated_str(src, preserve_whitespaces)
            self.assertEqual(test_dst, canonic_dst)


    def test_random(self):
        random_records = make_random_csv_records()
        for ir, rec in enumerate(random_records):
            canonic_fields = rec[0]
            escaped_entry = rec[1]
            canonic_warning = rec[2]
            test_fields, test_warning = rbql_utils.split_quoted_str(escaped_entry, ',')
            test_fields_preserved, test_warning_preserved = rbql_utils.split_quoted_str(escaped_entry, ',', True)
            self.assertEqual(','.join(test_fields_preserved), escaped_entry)
            self.assertEqual(canonic_warning, test_warning)
            self.assertEqual(test_warning_preserved, test_warning)
            self.assertEqual(test_fields, unquote_fields(test_fields_preserved))
            if not canonic_warning:
                self.assertEqual(canonic_fields, test_fields)


def make_random_csv_table(dst_path):
    random_records = make_random_csv_records()
    with open(dst_path, 'w') as dst:
        for rec in random_records:
            canonic_fields = rec[0]
            escaped_entry = rec[1]
            canonic_warning = rec[2]
            dst.write('{}\t{}\t{}\n'.format(escaped_entry, canonic_warning, ';'.join(canonic_fields)))


def test_random_csv_table(src_path):
    with open(src_path) as src:
        for iline, line in enumerate(src, 1):
            line = line.rstrip('\n')
            rec = line.split('\t')
            assert len(rec) == 3
            escaped_entry = rec[0]
            canonic_warning = int(rec[1])
            canonic_fields = rec[2].split(';')
            test_fields, test_warning = rbql_utils.split_quoted_str(escaped_entry, ',')
            test_fields_preserved, test_warning = rbql_utils.split_quoted_str(escaped_entry, ',', True)
            assert int(test_warning) == canonic_warning
            assert ','.join(test_fields_preserved) == escaped_entry
            if not canonic_warning:
                assert unquote_fields(test_fields_preserved) == test_fields
            if not canonic_warning and test_fields != canonic_fields:
                eprint("Error at line {} (1-based). Test fields: {}, canonic fields: {}".format(iline, test_fields, canonic_fields))
                sys.exit(1)



def make_random_bin_table(num_rows, num_cols, key_col1, key_col2, delim, dst_path):
    restricted_chars = ['\r', '\n'] + [delim]
    key_col = random.randint(0, num_cols - 1)
    good_keys1 = ['alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta']
    good_keys2 = [str(v) for v in range(20)]
    result_table = list()
    for r in rbql.xrange6(num_rows):
        result_table.append(list())
        for c in rbql.xrange6(num_cols):
            if c == key_col1:
                result_table[-1].append(random.choice(good_keys1))
            elif c == key_col2:
                result_table[-1].append(random.choice(good_keys2))
            else:
                dice = random.randint(1, 20)
                if dice == 1:
                    result_table[-1].append(random.choice(good_keys1))
                elif dice == 2:
                    result_table[-1].append(random.choice(good_keys2))
                else:
                    result_table[-1].append(make_random_csv_entry(0, 20, restricted_chars))
    with codecs.open(dst_path, 'w', encoding='latin-1') as f:
        for row in result_table:
            f.write(delim.join(row))
            f.write(random.choice(line_separators))


def system_has_node_js():
    import subprocess
    exit_code = 0
    out_data = ''
    try:
        cmd = ['node', '--version']
        pobj = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out_data, err_data = pobj.communicate()
        exit_code = pobj.returncode
    except OSError as e:
        if e.errno == 2:
            return False
        raise
    return exit_code == 0 and len(out_data) and len(err_data) == 0


def setUpModule():
    has_node = system_has_node_js()
    if not has_node:
        eprint('Warning: Node.js was not found, skipping js unit tests')
        global TEST_JS
        TEST_JS = False


def line_iter_split(src, chunk_size):
    line_iterator = rbql_utils.LineIterator(io.StringIO(src), chunk_size)
    result = []
    while True:
        row = line_iterator.get_row()
        if row is None:
            break
        result.append(row)
    return result


class TestLineSplit(unittest.TestCase):

    def test_split_custom(self):
        test_cases = list()
        test_cases.append(('', []))
        test_cases.append(('hello', ['hello']))
        test_cases.append(('hello\nworld', ['hello', 'world']))
        test_cases.append(('hello\rworld\n', ['hello', 'world']))
        test_cases.append(('hello\r\nworld\r', ['hello', 'world']))
        for tc in test_cases:
            src, canonic_res = tc
            test_res = line_iter_split(src, 6)
            self.assertEqual(canonic_res, test_res)

    def test_split_random(self):
        source_tokens = ['', 'defghIJKLMN', 'a', 'bc'] + ['\n', '\r\n', '\r']
        for test_case in rbql.xrange6(10000):
            num_tokens = random.randint(0, 12)
            chunk_size = random.randint(1, 5) if random.randint(0, 1) else random.randint(1, 100)
            src = ''
            for tnum in rbql.xrange6(num_tokens):
                token = random.choice(source_tokens)
                src += token
            test_split = line_iter_split(src, chunk_size)
            canonic_split = src.splitlines()
            self.assertEqual(canonic_split, test_split)


class TestParsing(unittest.TestCase):

    def test_literals_replacement(self):
        #TODO generate some random examples: Generate some strings randomly and then parse them
        test_cases = list()
        test_cases.append((r'Select 100 order by a1', []))
        test_cases.append((r'Select "hello" order by a1', ['"hello"']))
        test_cases.append((r"Select 'hello', 100 order by a1 desc", ["'hello'"]))
        test_cases.append((r'Select "hello", *, "world" 100 order by a1 desc', ['"hello"', '"world"']))
        test_cases.append((r'Select "hello", "world", "hello \" world", "hello \\\" world", "hello \\\\\\\" world" order by "world"', ['"hello"', '"world"', r'"hello \" world"', r'"hello \\\" world"', r'"hello \\\\\\\" world"', '"world"']))

        for tc in test_cases:
            format_expression, string_literals = rbql.separate_string_literals_py(tc[0])
            canonic_literals = tc[1]
            self.assertEqual(canonic_literals, string_literals)
            self.assertEqual(tc[0], rbql.combine_string_literals(format_expression, string_literals))


    def test_separate_actions(self):
        query = 'select top   100 *, a2, a3 inner  join /path/to/the/file.tsv on a1 == b3 where a4 == "hello" and int(b3) == 100 order by int(a7) desc '
        canonic_res = {'JOIN': {'text': '/path/to/the/file.tsv on a1 == b3', 'join_subtype': rbql.INNER_JOIN}, 'SELECT': {'text': '*, a2, a3', 'top': 100}, 'WHERE': {'text': 'a4 == "hello" and int(b3) == 100'}, 'ORDER BY': {'text': 'int(a7)', 'reverse': True}}
        test_res = rbql.separate_actions(query)
        assert test_res == canonic_res


    def test_except_parsing(self):
        except_part = '  a1,a2,a3, a4,a5, a6 ,   a7  ,a8'
        self.assertEqual('select_except(afields, [0,1,2,3,4,5,6,7])', rbql.translate_except_expression(except_part))

        except_part = 'a1 ,  a2,a3, a4,a5, a6 ,   a7  , a8  '
        self.assertEqual('select_except(afields, [0,1,2,3,4,5,6,7])', rbql.translate_except_expression(except_part))

        except_part = 'a1'
        self.assertEqual('select_except(afields, [0])', rbql.translate_except_expression(except_part))


    def test_join_parsing(self):
        join_part = '/path/to/the/file.tsv on a1 == b3'
        self.assertEqual(('/path/to/the/file.tsv', 'safe_join_get(afields, 1)', 'safe_join_get(bfields, 3)'), rbql.parse_join_expression(join_part))

        join_part = ' file.tsv on b20== a12  '
        self.assertEqual(('file.tsv', 'safe_join_get(afields, 12)', 'safe_join_get(bfields, 20)'), rbql.parse_join_expression(join_part))

        join_part = '/path/to/the/file.tsv on a1==a12  '
        with self.assertRaises(Exception) as cm:
            rbql.parse_join_expression(join_part)
        e = cm.exception
        self.assertTrue(str(e).find('Invalid join syntax') != -1)

        join_part = ' Bon b1 == a12 '
        with self.assertRaises(Exception) as cm:
            rbql.parse_join_expression(join_part)
        e = cm.exception
        self.assertTrue(str(e).find('Invalid join syntax') != -1)


    def test_update_translation(self):
        rbql_src = '  a1 =  a2  + b3, a2=a4  if b3 == a2 else a8, a8=   100, a30  =200/3 + 1  '
        test_dst = rbql.translate_update_expression(rbql_src, '    ')
        canonic_dst = list()
        canonic_dst.append('safe_set(up_fields, 1,  a2  + b3)')
        canonic_dst.append('    safe_set(up_fields, 2,a4  if b3 == a2 else a8)')
        canonic_dst.append('    safe_set(up_fields, 8,   100)')
        canonic_dst.append('    safe_set(up_fields, 30,200/3 + 1)')
        canonic_dst = '\n'.join(canonic_dst)
        self.assertEqual(canonic_dst, test_dst)


    def test_select_translation(self):
        rbql_src = ' *, a1,  a2,a1,*,*,b1, * ,   * '
        test_dst = rbql.translate_select_expression_py(rbql_src)
        canonic_dst = '[] + star_fields + [ a1,  a2,a1] + star_fields + [] + star_fields + [b1] + star_fields + [] + star_fields + []'
        self.assertEqual(canonic_dst, test_dst)

        rbql_src = ' *, a1,  a2,a1,*,*,*,b1, * ,   * '
        test_dst = rbql.translate_select_expression_py(rbql_src)
        canonic_dst = '[] + star_fields + [ a1,  a2,a1] + star_fields + [] + star_fields + [] + star_fields + [b1] + star_fields + [] + star_fields + []'
        self.assertEqual(canonic_dst, test_dst)

        rbql_src = ' * '
        test_dst = rbql.translate_select_expression_py(rbql_src)
        canonic_dst = '[] + star_fields + []'
        self.assertEqual(canonic_dst, test_dst)

        rbql_src = ' *,* '
        test_dst = rbql.translate_select_expression_py(rbql_src)
        canonic_dst = '[] + star_fields + [] + star_fields + []'
        self.assertEqual(canonic_dst, test_dst)

        rbql_src = ' *,*, * '
        test_dst = rbql.translate_select_expression_py(rbql_src)
        canonic_dst = '[] + star_fields + [] + star_fields + [] + star_fields + []'
        self.assertEqual(canonic_dst, test_dst)

        rbql_src = ' *,*, * , *'
        test_dst = rbql.translate_select_expression_py(rbql_src)
        canonic_dst = '[] + star_fields + [] + star_fields + [] + star_fields + [] + star_fields + []'
        self.assertEqual(canonic_dst, test_dst)



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--create_random_binary_table', metavar='FILE', help='create random binary table and write it to FILE')
    parser.add_argument('--create_random_csv_table', metavar='FILE', help='create random csv table and write it to FILE')
    parser.add_argument('--test_random_csv_table', metavar='FILE', help='test split method using samples from FILE')
    args = parser.parse_args()
    if args.create_random_binary_table is not None:
        dst_path = args.create_random_binary_table
        make_random_bin_table(1000, 4, 1, 3, '\t', dst_path)
    if args.create_random_csv_table is not None:
        dst_path = args.create_random_csv_table
        make_random_csv_table(dst_path)
    if args.test_random_csv_table is not None:
        src_path = args.test_random_csv_table
        test_random_csv_table(src_path)



if __name__ == '__main__':
    main()

