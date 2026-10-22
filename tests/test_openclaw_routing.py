"""Routing regression tests without logging into Xianyu or contacting an LLM."""
import ast
import asyncio
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bridge_message_queue import BridgeMessageQueue
from utils.openclaw_handoff import build_bridge_message


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cross_thread_sse_delivery(self):
        queue = BridgeMessageQueue()
        subscriber = queue.subscribe('seller')
        waiting = asyncio.create_task(subscriber.get())
        await asyncio.sleep(0)
        await asyncio.to_thread(lambda: asyncio.run(queue.publish('seller', {'content': 'hello'})))
        self.assertEqual((await asyncio.wait_for(waiting, 1))['content'], 'hello')
        queue.unsubscribe('seller', subscriber)
        self.assertEqual(queue.get_subscriber_count('seller'), 0)

    async def test_exclusive_handoff_and_no_local_fallback(self):
        tree = ast.parse(Path('XianyuAutoAsync.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'XianyuLive')
        method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)
                      and n.name == '_process_chat_message_reply')
        bridge = Mock(publish=AsyncMock(), get_subscriber_count=Mock(return_value=0))
        namespace = {'AUTO_REPLY': {'enabled': True}, 'BRIDGE_ENABLED': True,
                     'bridge_queue': bridge, 'logger': Mock()}
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<actual-routing-method>', 'exec'), namespace)
        db = Mock()
        db.get_item_info.return_value = {'item_title': 'bag'}
        db.get_ai_reply_settings.return_value = {'custom_prompts': 'be concise', 'api_key': 'secret'}
        module = types.ModuleType('db_manager')
        module.db_manager = db
        live = types.SimpleNamespace(cookie_id='seller', _safe_str=str,
                                     get_ai_reply=AsyncMock(side_effect=AssertionError('local LLM called')))
        with patch.dict(sys.modules, {'db_manager': module}), patch('utils.commerce_automation.get_store') as get_store:
            get_store.return_value.history.return_value = []
            await namespace[method.name](live, {'id': 'one'}, None, 'buyer', 'buyer-id', 'hello', 'item', 'chat', 'now')
            event = bridge.publish.await_args.args[1]
            self.assertEqual(event['replyContext']['sellerInstructions'], 'be concise')
            self.assertNotIn('secret', str(event))
            bridge.publish.assert_awaited_once()
            namespace['BRIDGE_ENABLED'] = False
            await namespace[method.name](live, {}, None, 'buyer', 'buyer-id', 'hello', 'item', 'chat', 'now')
            bridge.publish.assert_awaited_once()
        live.get_ai_reply.assert_not_awaited()

    def test_stable_message_id_and_secret_exclusion(self):
        a = build_bridge_message('s', 'c', 'b', 'buyer', 'hi', 'i', {'id': 1},
                                 {'item_title': 'bag'}, {'api_key': 'secret', 'custom_prompts': 'brief'})
        b = build_bridge_message('s', 'c', 'b', 'buyer', 'hi', 'i', {'id': 1})
        self.assertEqual(a['messageId'], b['messageId'])
        self.assertNotIn('secret', str(a))


if __name__ == '__main__':
    unittest.main()
