import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET

from domestic import DomesticReview, _Journal, _resume_description, item_description
from live_platform import Page
from pipeline import domestic_order
from student_document import DocumentReview


NAME = '全能组合螺丝刀'
SPEC = '制式:公制,磁性:不带磁性'
FULL = NAME + ' ' + SPEC
BASE = 'https://school.example/simtrade/'
DOC = BASE + 'doc/M12.aspx'
CT = 'FIXTURE'
ITEM = dict(M1303='22003', M1304=FULL, M1305='SET', M1306='2', M1307='8.50', M1308='17.00')


class DraftDesk:
    role = 'exporter'
    account = 'BUYER'

    def __init__(self):
        self.mode = '1'
        main = ET.Element('M12')
        for key, value in dict(M1201=CT,M1243='BUYER1',M1245='FACTORY3',M1214='17').items():
            ET.SubElement(main, key).text = value
        self.headers = ET.Element('ROOT')
        self.headers.append(main)
        self.lines = ET.Element('ROOT')
        line = ET.SubElement(self.lines, 'M13')
        for key,value in dict(ITEM, M1301=CT,M1302='71',M1304=SPEC,M1306='2.000',M1309='RMB').items():
            ET.SubElement(line, key).text = value
        self.posts = 0
        self.lose_response = False

    def get(self, url):
        path = urlsplit(url).path.rsplit('/', 1)[1]
        if path == 'head.xml' or path == 'lines.xml':
            xml = self.headers if path == 'head.xml' else self.lines
            return Page(url, ET.tostring(xml, encoding='unicode'))
        if path == 'M12.aspx':
            return Page(url, f'''<input name="flag" value="{self.mode}"><input name="flag2" value="{CT}">
                <input name="flag4" value="BUYER1"><xml id="source" src="head.xml"></xml>
                <xml id="sourceChild" src="lines.xml"></xml>
                <a onclick="DetailOperate(1,this)">修改</a>
                <script>var link="CM13.aspx?a1="+ct+"&a2="+sku+"&a3="+row;
                doClientSubmit("POST","PostForm.aspx?tb=M12&uid="+user);</script>''')
        if path == 'CM13.aspx' and parse_qs(urlsplit(url).query) == {'a1':[CT],'a2':['22003'],'a3':['1']}:
            return Page(url, f'''<input name="M1301" value="{CT}"><input name="flag" value="1">
                <input name="flag2" value="1"><textarea datafld="M1304" datasrc="#theXMLisland"></textarea>''')
        raise AssertionError('Unexpected request ' + path)

    def post_xml(self, page, target, payload):
        if target != BASE + 'doc/PostForm.aspx?tb=M12&uid=BUYER1':
            raise AssertionError('Unexpected save endpoint')
        root = ET.fromstring(payload)
        self.lines[:] = root.findall('M13')
        self.headers[:] = root.findall('M12')
        self.posts += 1
        if self.lose_response:
            raise ConnectionError('Saved but lost receipt')
        return Page(target, '0 OK')


class DomesticDescriptionTests(unittest.TestCase):
    def test_quote_uses_name_and_spec_from_catalogue(self):
        self.assertEqual(item_description(NAME, SPEC), FULL)
        self.assertEqual(item_description(NAME, FULL), FULL)
        case = dict(product=dict(id='22003',name_cn=NAME,spec_cn=SPEC,unit='SET',quantity=2,cartons=1),
                    domestic=dict(contract=CT,unit_price_rmb='8.5',production_unit_cost_rmb='5',
                                  signing_city='签约地',delivery_deadline='2030-01-09'),
                    economics=dict(factory_profit_RMB='5'),cash_floor=dict(exporter='100',factory='100'))
        self.assertEqual(domestic_order(case)['description_cn'], FULL)
        self.assertEqual(domestic_order(case)['legacy_description_cn'], SPEC)

    def setUp(self):
        self.desk = DraftDesk()
        self.factory = SimpleNamespace(account='FACTORY')
        self.status = Mock(return_value='起草合同')
        self.edit = Mock(return_value=DOC)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name, 'journal.json')
        self.identity = dict(platform=BASE, buyer='BUYER', factory='FACTORY',
                             order=dict(contract=CT, description_cn=FULL, unit_price_rmb='8.50'))
        old = dict(self.identity, order=dict(self.identity['order'], description_cn=SPEC))
        self.path.write_text(json.dumps(dict(identity=old,steps=dict(draft={'status':'done'}))),encoding='utf-8')

    def resume(self, journal=None):
        if journal is None:
            journal = _Journal(self.path, self.identity, SPEC)
        return _resume_description(journal, self.desk, self.factory, {'M1214':'17.00'}, ITEM, self.status, self.edit)

    def test_unsigned_draft_changes_one_cell_in_original_row_and_can_resume(self):
        expected = ET.fromstring(ET.tostring(self.desk.lines))
        expected.find('M13/M1304').text = FULL
        self.resume()
        self.assertEqual(ET.tostring(self.desk.lines), ET.tostring(expected))
        self.resume()
        self.assertEqual(self.desk.posts, 1)
        self.assertEqual(_Journal(self.path, self.identity).data['identity'], self.identity)

    def test_lost_save_receipt_recovers_without_posting_again(self):
        self.desk.lose_response = True
        with self.assertRaises(ConnectionError):
            self.resume()
        self.assertEqual(_Journal(self.path, self.identity, SPEC).data['identity']['order']['description_cn'], SPEC)
        self.resume()
        self.assertEqual(self.desk.posts, 1)
        self.assertEqual(_Journal(self.path, self.identity).data['identity'], self.identity)

    def test_pending_or_completed_send_preserves_existing_terms(self):
        journal = _Journal(self.path, self.identity, SPEC)
        for state in ('pending', 'done'):
            with self.subTest(state=state):
                journal.data['steps']['send'] = {'status':state}
                self.assertEqual(self.resume(journal)['M1304'], SPEC)
        self.edit.assert_not_called()
        self.status.assert_not_called()
        self.assertEqual(self.desk.posts, 0)

    def test_refuses_unexpected_status_amount_or_item(self):
        self.status.return_value = '正在进行'
        with self.assertRaises(DomesticReview):
            self.resume()
        self.status.return_value = '起草合同'
        self.desk.mode = '3'
        with self.assertRaises(DocumentReview):
            self.resume()
        self.desk.mode = '1'
        self.desk.lines.find('M13/M1307').text = '9'
        with self.assertRaises(DocumentReview):
            self.resume()
        self.desk.lines.append(ET.Element('M13'))
        with self.assertRaises(DocumentReview):
            self.resume()
        self.assertEqual(self.desk.posts, 0)

    def test_legacy_identity_still_rejects_other_economic_terms(self):
        altered = dict(self.identity, order=dict(self.identity['order'],unit_price_rmb='99'))
        with self.assertRaises(DomesticReview):
            _Journal(self.path, altered, SPEC)


if __name__ == '__main__':
    unittest.main()
