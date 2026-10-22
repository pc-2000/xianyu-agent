"""Replay captured trade events through real code with fake WS/confirmation/Agent IO.

Usage: python scripts/replay_payment_log.py logs/payment-event-stdout.log
No real credentials, network, messages or order mutations are used.
"""
import ast
import asyncio
import base64
import json
from pathlib import Path
import re
import sys
import tempfile
import types
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.commerce_automation import CommerceStore, parse_trade_event


async def main():
    tree = ast.parse(Path('XianyuAutoAsync.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'XianyuLive')
    names = {'_handle_commerce_trade_event', 'send_msg'}
    methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    queue = Mock(publish=AsyncMock())
    ns = dict(json=json, base64=base64, logger=Mock(), generate_mid=lambda: 'm', generate_uuid=lambda: 'u',
              BRIDGE_ENABLED=True, bridge_queue=queue)
    exec(compile(ast.Module(body=methods, type_ignores=[]), '<actual-methods>', 'exec'), ns)
    live = type('Live', (), {n: ns[n] for n in names})()
    live.auto_confirm = AsyncMock(return_value={'success': True})
    ws = Mock(send=AsyncMock())
    expected = set(); count = 0
    with tempfile.TemporaryDirectory() as tmp:
        store = CommerceStore(Path(tmp) / 'test.db')
        module = types.ModuleType('db_manager')
        db = module.db_manager = Mock()
        db.get_order_by_id.return_value = None
        db.get_item_info.return_value = {'item_title': 'Replay product', 'item_price': '100'}
        db.get_ai_reply_settings.return_value = {}
        with patch.dict(sys.modules, {'db_manager': module}), patch('utils.commerce_automation.get_store', return_value=store):
            for line in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines():
                line = re.sub(r'\x1b\[[0-9;]*m', '', line)
                start = line.find("{'1':")
                if start < 0: continue
                try:
                    message = ast.literal_eval(line[start:])
                    receiver = message['1']['10']['receiver']
                except (TypeError, KeyError, ValueError, SyntaxError): continue
                event = parse_trade_event(message, receiver)
                if not event: continue
                live.cookie_id = live.myid = receiver
                store.save_config(receiver, event['item_id'], True, 'REPLAY_TEST_CONTENT')
                if event['status'] == 'paid': expected.add((receiver, event['order_id']))
                assert await live._handle_commerce_trade_event(ws, message)
                count += 1
        assert count > 0, 'No captured trade events parsed'
        assert ws.send.await_count == len(expected), (ws.send.await_count, len(expected))
        assert live.auto_confirm.await_count == len(expected)
        for call in ws.send.await_args_list:
            packet = json.loads(call.args[0])
            payload = json.loads(base64.b64decode(packet['body'][0]['content']['custom']['data']))
            assert payload['text']['text'] == 'REPLAY_TEST_CONTENT'
        print(f'Trade events: {count}; paid orders: {len(expected)}; simulated sends: {ws.send.await_count}; confirmations: {live.auto_confirm.await_count}; Agent events: {queue.publish.await_count}')


if __name__ == '__main__': asyncio.run(main())
