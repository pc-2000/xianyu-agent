import ast
import asyncio
import base64
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from utils.commerce_automation import CommerceStore, complete_trade_event, money, parse_trade_event, quoted_seller_price
from utils.order_price_changer import extract_order_price


def card(paid=True, buyer='buyer', order='1234567890123456789', seller='seller', item='item',
         update_key=None, reminder=None, content=None):
    update_key = update_key or (f'chat:{order}:63:TRADE_PAID_DONE_SELLER:26' if paid else f'chat:{order}:1_not_pay_seller')
    return {'1': {'2': 'chat@goofish', '5': 1234567890000, '6': {'3': {'5': json.dumps({'contentType': 26})}},
                  '10': {'receiver': seller, 'senderUserId': buyer,
                         'reminderContent': content or ('[我已付款，等待你发货]' if paid else '[我已拍下，待付款]'),
                         'redReminder': reminder or ('等待卖家发货' if paid else '等待买家付款'),
                         'reminderUrl': f'fleamarket://message_chat?itemId={item}&peerUserId={buyer}',
                         'extJson': json.dumps({'updateKey': update_key})}}}


def map_update(order='1234567890123456789', seller='seller', item='item', buyer='buyer',
               update_key=None, button='已付款', reminder='等待买家付款', content='[已付款，待发货]'):
    update_key = update_key or f'chat:{order}:1_not_pay_seller'
    return {'1': '4363299084275.PNM', '2': 'chat@goofish', '3': 1, '4': {
        '_CONTENT_MAP_UPDATE_PRE_dxCard.item.main.exContent.button': json.dumps(
            {'text': button, 'targetUrl': f'fleamarket://order_detail?id={order}&role=seller'}),
        'extJson': json.dumps({'updateKey': update_key, 'contentType': '26'}),
        'receiver': seller, 'senderUserId': buyer, 'redReminder': reminder, 'reminderContent': content,
        'reminderUrl': f'fleamarket://message_chat?itemId={item}&peerUserId={buyer}'}, '5': 1234567890000}


def skinny_paid(chat='chat'):
    return {'1': f'{chat}@goofish', '2': 1, '3': {'redReminder': '等待卖家发货', 'redReminderStyle': '1'}, '4': 1}


class CommerceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CommerceStore(Path(self.temp.name) / 'commerce.db')
        self.event = parse_trade_event(card(), 'seller')
        self.send = AsyncMock()
        self.confirm = AsyncMock(return_value={'success': True})
        self.store.save_config('account', 'item', True, 'https://pan.example/test 提取码 abcd')

    async def deliver(self):
        return await self.store.deliver('account', self.event, self.send, self.confirm)

    def test_platform_card_paid_and_unpaid_are_distinct(self):
        self.assertEqual(parse_trade_event(card(), 'seller')['status'], 'paid')
        self.assertEqual(parse_trade_event(card(False), 'seller')['status'], 'pending')
        self.assertIsNone(parse_trade_event(card(), 'other-seller'))
        for mutate in ('content', 'receiver', 'chat', 'marker'):
            message = copy.deepcopy(card())
            if mutate == 'content': message['1']['6']['3']['5'] = json.dumps({'contentType': 1})
            if mutate == 'receiver': message['1']['10']['receiver'] = 'other'
            if mutate == 'chat': message['1']['2'] = 'another@goofish'
            if mutate == 'marker': message['1']['10']['extJson'] = '{}'
            self.assertIsNone(parse_trade_event(message, 'seller'))

    def test_real_payment_patches_and_reminders_are_paid(self):
        order = '3317147400119062784'
        remind = card(update_key=f'chat:{order}:103:REMIND_DELIVER_SELLER:26',
                      reminder='等待卖家发货', content='[记得及时发货]', order=order)
        self.assertEqual(parse_trade_event(remind, 'seller')['status'], 'paid')
        self.assertEqual(parse_trade_event(map_update(
            order=order, update_key=f'chat:{order}:63:TRADE_PAID_DONE_SELLER:26',
            button='去发货', reminder='等待卖家发货', content='[我已付款，等待你发货]'), 'seller')['status'], 'paid')
        self.assertEqual(parse_trade_event(map_update(order=order), 'seller')['status'], 'paid')
        self.assertEqual(parse_trade_event(skinny_paid(), 'seller')['status'], 'paid')
        self.assertEqual(parse_trade_event(skinny_paid(), 'seller')['order_id'], '')
        buyer_text = copy.deepcopy(card())
        buyer_text['1']['6']['3']['5'] = json.dumps({'contentType': 1, 'text': {'text': '我已付款'}})
        self.assertIsNone(parse_trade_event(buyer_text, 'seller'))
        self.assertIsNone(complete_trade_event(parse_trade_event(skinny_paid(), 'seller'), self.store, 'account'))
        self.store.record_trade('account', parse_trade_event(card(False), 'seller'))
        filled = complete_trade_event(parse_trade_event(skinny_paid(), 'seller'), self.store, 'account')
        self.assertEqual(filled['order_id'], '1234567890123456789')
        self.assertEqual(filled['status'], 'paid')

    async def test_paid_sends_configured_content_then_confirms_once(self):
        calls = []
        async def send(text): calls.append(('send', text))
        async def confirm(): calls.append(('confirm', None)); return {'success': True}
        self.assertEqual(await self.store.deliver('account', self.event, send, confirm), 'completed')
        self.assertEqual(calls, [('send', 'https://pan.example/test 提取码 abcd'), ('confirm', None)])
        self.assertEqual(await self.deliver(), 'completed')
        self.send.assert_not_awaited()
        self.confirm.assert_not_awaited()

    async def test_unpaid_unconfigured_disabled_do_not_deliver(self):
        pending = parse_trade_event(card(False), 'seller')
        self.assertEqual(await self.store.deliver('account', pending, self.send, self.confirm), 'not_paid')
        self.assertEqual(await self.store.deliver('another-account', self.event, self.send, self.confirm), 'not_configured')
        self.store.save_config('account', 'item', False, 'secret')
        self.assertEqual(await self.deliver(), 'not_configured')
        self.send.assert_not_awaited(); self.confirm.assert_not_awaited()

    async def test_concurrent_payment_and_restart_do_not_duplicate(self):
        async def send(text): await asyncio.sleep(.01)
        self.send.side_effect = send
        await asyncio.gather(self.deliver(), self.deliver())
        self.send.assert_awaited_once(); self.confirm.assert_awaited_once()
        reopened = CommerceStore(self.store.path)
        self.assertEqual(await reopened.deliver('account', self.event, self.send, self.confirm), 'completed')
        self.send.assert_awaited_once()

    async def test_confirm_failure_retries_confirmation_without_resending(self):
        self.confirm.return_value = {'error': 'rejected'}
        self.assertEqual(await self.deliver(), 'confirm_failed')
        self.confirm.return_value = {'success': True}
        self.assertEqual(await self.deliver(), 'completed')
        self.send.assert_awaited_once(); self.assertEqual(self.confirm.await_count, 2)

    async def test_uncertain_send_never_auto_retries(self):
        self.send.side_effect = RuntimeError('socket closed')
        with self.assertRaises(RuntimeError): await self.deliver()
        self.assertEqual(await self.deliver(), 'send_uncertain')
        self.send.assert_awaited_once(); self.confirm.assert_not_awaited()

    async def test_actual_ws_handler_routes_unpaid_to_agent_and_paid_to_configured_delivery(self):
        tree = ast.parse(Path('XianyuAutoAsync.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'XianyuLive')
        names = {'_handle_commerce_trade_event', 'send_msg'}
        methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        queue = Mock(publish=AsyncMock())
        queue.get_subscriber_count.return_value = 1
        namespace = {'logger': Mock(), 'BRIDGE_ENABLED': True, 'bridge_queue': queue, 'json': json,
                     'base64': base64, 'generate_mid': lambda: 'mid', 'generate_uuid': lambda: 'uuid'}
        exec(compile(ast.Module(body=methods, type_ignores=[]), '<actual-ws-handler>', 'exec'), namespace)
        live = type('Live', (), {name: namespace[name] for name in names})()
        live.cookie_id = 'account'; live.myid = 'seller'; live.auto_confirm = self.confirm
        db_module = types.ModuleType('db_manager')
        db = db_module.db_manager = Mock()
        db.get_order_by_id.return_value = None
        db.get_item_info.return_value = {'item_title': '资料', 'item_price': '100'}
        db.get_ai_reply_settings.return_value = {'custom_prompts': '礼貌回复'}
        ws = Mock(send=AsyncMock())
        with patch.dict(sys.modules, {'db_manager': db_module}), \
             patch('utils.commerce_automation.get_store', return_value=self.store):
            self.assertTrue(await live._handle_commerce_trade_event(ws, card(False)))
            self.assertTrue(await live._handle_commerce_trade_event(ws, card(False)))
            queue.publish.assert_awaited_once()
            outbound = queue.publish.await_args.args[1]
            self.assertEqual(outbound['conversationId'], 'chat')
            self.assertEqual(outbound['senderId'], 'buyer')
            self.assertEqual(outbound['contentType'], 'trade')
            self.assertIn('尚未付款', outbound['content'])
            self.assertEqual(outbound['replyContext']['order']['orderId'], self.event['order_id'])
            ws.send.assert_not_awaited(); self.confirm.assert_not_awaited()
            self.assertTrue(await live._handle_commerce_trade_event(ws, card()))
            self.assertTrue(await live._handle_commerce_trade_event(ws, card()))
            self.assertEqual(queue.publish.await_count, 2)
            paid_notice = queue.publish.await_args.args[1]['content']
            self.assertIn('已经付款', paid_notice)
            self.assertIn('客套话', paid_notice)
            self.assertIn('不要重复发送', paid_notice)
        ws.send.assert_awaited_once(); self.confirm.assert_awaited_once()
        packet = json.loads(ws.send.await_args.args[0])
        self.assertEqual(packet['body'][0]['cid'], 'chat@goofish')
        self.assertEqual(packet['body'][1]['actualReceivers'], ['buyer@goofish', 'seller@goofish'])
        content = json.loads(base64.b64decode(packet['body'][0]['content']['custom']['data']))
        self.assertEqual(content['text']['text'], 'https://pan.example/test 提取码 abcd')

    async def test_map_update_after_unpaid_card_delivers_once(self):
        tree = ast.parse(Path('XianyuAutoAsync.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'XianyuLive')
        names = {'_handle_commerce_trade_event', 'send_msg'}
        methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        queue = Mock(publish=AsyncMock())
        queue.get_subscriber_count.return_value = 1
        namespace = {'logger': Mock(), 'BRIDGE_ENABLED': True, 'bridge_queue': queue, 'json': json,
                     'base64': base64, 'generate_mid': lambda: 'mid', 'generate_uuid': lambda: 'uuid'}
        exec(compile(ast.Module(body=methods, type_ignores=[]), '<actual-ws-handler>', 'exec'), namespace)
        live = type('Live', (), {name: namespace[name] for name in names})()
        live.cookie_id = 'account'; live.myid = 'seller'; live.auto_confirm = self.confirm
        db_module = types.ModuleType('db_manager')
        db = db_module.db_manager = Mock()
        db.get_order_by_id.return_value = None
        db.get_item_info.return_value = {'item_title': '资料', 'item_price': '100'}
        db.get_ai_reply_settings.return_value = {'custom_prompts': '礼貌回复'}
        ws = Mock(send=AsyncMock())
        with patch.dict(sys.modules, {'db_manager': db_module}), \
             patch('utils.commerce_automation.get_store', return_value=self.store):
            self.assertTrue(await live._handle_commerce_trade_event(ws, card(False)))
            self.assertTrue(await live._handle_commerce_trade_event(ws, skinny_paid()))
            self.assertTrue(await live._handle_commerce_trade_event(ws, map_update()))
        ws.send.assert_awaited_once()
        self.confirm.assert_awaited_once()

    def test_config_is_scoped_and_validated(self):
        self.assertFalse(self.store.config('other', 'item')['enabled'])
        for args in [(True, '', False, '0'), (False, '', True, '0'), (False, '', True, 'NaN')]:
            with self.assertRaises(ValueError): self.store.save_config('account', 'x', *args)
        for value in ('NaN', 'Infinity', '-1', '1.001', 'bad'):
            with self.assertRaises(ValueError): money(value)

    async def test_upgrade_preserves_old_delivery_deduplication(self):
        old_db = Path(self.temp.name) / 'old.db'
        with closing(sqlite3.connect(old_db)) as conn:
            conn.execute('CREATE TABLE payment_notification_replies (account_id TEXT,order_id TEXT,status TEXT)')
            conn.execute('INSERT INTO payment_notification_replies VALUES (?,?,?)', ('account', self.event['order_id'], 'sent'))
            conn.commit()
        self.store.import_legacy_replies(old_db)
        self.assertEqual(await self.deliver(), 'legacy_sent')
        self.send.assert_not_awaited(); self.confirm.assert_not_awaited()

    def test_restart_marks_uncertain_work_for_manual_review(self):
        with self.store.db() as conn:
            conn.execute('INSERT INTO delivery_jobs VALUES (?,?,?,?,?,?,?,?,?,?)',
                         ('account', 'o', 'item', 'buyer', 'chat', 'content', 'sending', '', 1, 1))
        reopened = CommerceStore(self.store.path, recover=True)
        self.assertEqual(reopened.jobs(['account'])[0]['status'], 'send_uncertain')

    def test_paid_event_cannot_be_regressed_by_late_unpaid_event(self):
        first = self.store.record_trade('account', self.event)
        pending = parse_trade_event(card(False), 'seller')
        second = self.store.record_trade('account', pending)
        self.assertEqual(second['status'], 'paid')
        self.assertEqual(first['token'], second['token'])
        conflict = dict(pending, buyer_id='someone-else')
        with self.assertRaises(ValueError): self.store.record_trade('account', conflict)

    def test_offline_order_handoff_is_durable_and_not_replayed_after_payment(self):
        event = parse_trade_event(card(False), 'seller')
        self.store.record_trade('account', event)
        self.store.save_handoff('account', event['order_id'], {'orderContext': {'orderId': event['order_id']}})
        reopened = CommerceStore(self.store.path)
        self.assertEqual(len(reopened.pending_handoffs('account')), 1)
        self.assertEqual(reopened.pending_handoffs('other'), [])
        reopened.record_trade('account', self.event)
        self.assertEqual(reopened.pending_handoffs('account'), [])

    def agreement(self):
        event = self.store.record_trade('account', parse_trade_event(card(False), 'seller'))
        self.store.save_config('account', 'item', False, '', True, '90')
        seller = self.store.record_message('account', 'chat', 'buyer', 'item', 'seller', '最低90元，可以的话拍下后改价')
        buyer = self.store.record_message('account', 'chat', 'buyer', 'item', 'buyer', '好的，按90元成交')
        return event['token'], seller, buyer

    def validate(self, token, seller, buyer, price='90'):
        return self.store.validate_price(token, price, seller, buyer,
                                         {'max_discount_percent': 10, 'max_discount_amount': 20}, '100')

    def test_agreed_price_with_real_evidence_passes(self):
        token, seller, buyer = self.agreement()
        event, price = self.validate(token, seller, buyer)
        self.assertEqual(str(price), '90'); self.assertEqual(event['buyer_id'], 'buyer')
        self.assertTrue(self.store.claim_price('account', event['order_id'], price))
        self.assertFalse(self.store.claim_price('account', event['order_id'], price))

    def test_price_requires_floor_real_evidence_and_unpaid_status(self):
        token, seller, buyer = self.agreement()
        for price, seller_id, buyer_id in [('89', seller, buyer), ('0', seller, buyer),
                                           ('90', 'invented', buyer), ('90', buyer, seller)]:
            with self.assertRaises(ValueError): self.validate(token, seller_id, buyer_id, price)
        self.store.record_trade('account', self.event)
        with self.assertRaises(ValueError): self.validate(token, seller, buyer)

    def test_buyer_counteroffer_or_other_item_not_agreement(self):
        token, seller, _ = self.agreement()
        for content, item in [('可以，80元', 'item'), ('不行，太贵了', 'item'), ('好的', 'other')]:
            buyer = self.store.record_message('account', 'chat', 'buyer', item, 'buyer', content)
            with self.assertRaises(ValueError): self.validate(token, seller, buyer)

    def test_live_bargain_wording_accepts_offer_then_purchase_request(self):
        event = self.store.record_trade('account', parse_trade_event(card(False), 'seller'))
        self.store.save_config('account', 'item', False, '', True, '2.5')
        seller = self.store.record_message('account', 'chat', 'buyer', 'item', 'seller',
            '亲～2.5真的做不了呀，资料本身才3块，我最多再让一点——¥2.7给您，这已经是最低价了。')
        buyer = self.store.record_message('account', 'chat', 'buyer', 'item', 'buyer', '2.7 我拍下可以给我改价格吗')
        result = self.store.validate_price(event['token'], '2.7', seller, buyer,
            {'max_discount_percent': 10, 'max_discount_amount': 100}, '3')
        self.assertEqual(result[1], money('2.7'))

    def test_live_bargain_wording_accepts_buyer_offer_then_seller_confirmation(self):
        event = self.store.record_trade('account', parse_trade_event(card(False), 'seller'))
        self.store.save_config('account', 'item', False, '', True, '2.5')
        buyer = self.store.record_message('account', 'chat', 'buyer', 'item', 'buyer', '2.7 我拍下可以给我改价格吗')
        seller = self.store.record_message('account', 'chat', 'buyer', 'item', 'seller',
            '可以的亲～您直接拍下，我这边马上给您改成¥2.7，改好您再付款就行。')
        result = self.store.validate_price(event['token'], '2.7', seller, buyer,
            {'max_discount_percent': 10, 'max_discount_amount': 100}, '3')
        self.assertEqual(result[1], money('2.7'))

    def test_rejected_price_is_not_mistaken_for_an_offer(self):
        for text in ['¥2.7不行，最低¥2.8', '不能改成¥2.7', '不接受你说的¥2.7', '2.7元不卖']:
            self.assertFalse(quoted_seller_price(text, money('2.7')), text)
        self.assertTrue(quoted_seller_price('标价¥3，成交价¥2.7', money('2.7')))

    def test_generic_acceptance_before_a_later_offer_is_not_an_agreement(self):
        event = self.store.record_trade('account', parse_trade_event(card(False), 'seller'))
        self.store.save_config('account', 'item', False, '', True, '90')
        buyer = self.store.record_message('account', 'chat', 'buyer', 'item', 'buyer', '好的')
        seller = self.store.record_message('account', 'chat', 'buyer', 'item', 'seller', '90元可以')
        with self.assertRaises(ValueError): self.validate(event['token'], seller, buyer)

    def test_history_scope_and_outgoing_product_inference(self):
        self.store.record_message('account', 'chat', 'buyer', 'item', 'buyer', '90可以吗')
        self.store.record_message('other-account', 'chat', 'buyer', 'item', 'buyer', 'private')
        seller = self.store.record_message('account', 'chat', 'buyer', '', 'seller', '90可以')
        rows = self.store.history('account', 'chat', 'buyer')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]['id'], seller); self.assertEqual(rows[-1]['item_id'], 'item')

    def test_fresh_order_parser_requires_unpaid_and_one_item(self):
        data = {'utArgs': {'orderStatusName': '等待买家付款'}, 'components': [
            {'render': 'orderInfoVO', 'data': {'itemInfo': {'quantity': 1}, 'priceInfo': {'amount': {'value': '100.00'}}}}]}
        self.assertEqual(extract_order_price(data), '100.00')
        data['utArgs']['orderStatusName'] = '买家拍下了宝贝，还没付款哦'
        self.assertEqual(extract_order_price(data), '100.00')
        data['utArgs']['orderStatusName'] = '等待卖家发货'
        self.assertIsNone(extract_order_price(data))

    def test_order_version_conflict_releases_write_lock(self):
        tree = ast.parse(Path('db_manager.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DBManager')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'insert_or_update_order')
        namespace = {'logger': Mock()}
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<actual-order-update>', 'exec'), namespace)
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('CREATE TABLE orders (order_id TEXT PRIMARY KEY,item_id TEXT,updated_at TEXT,version INTEGER)')
            conn.execute("INSERT INTO orders (order_id,version) VALUES ('o',2)"); conn.commit()
            manager = types.SimpleNamespace(conn=conn, lock=threading.RLock())
            self.assertFalse(namespace[method.name](manager, 'o', item_id='item', expected_version=1))
            self.assertFalse(conn.in_transaction)


if __name__ == '__main__': unittest.main()
