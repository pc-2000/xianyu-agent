import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from commerce_api import create_commerce_router, create_order_tool_router
from utils.commerce_automation import CommerceStore


class CommerceApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = CommerceStore(Path(self.temp.name) / 'test.db')
        self.db = Mock()
        self.db.get_all_cookies.return_value = {'seller': 'cookie-not-returned'}
        self.db.get_items_by_cookie.return_value = [{'cookie_id': 'seller', 'item_id': 'item', 'item_title': '资料', 'item_price': '100'}]
        self.db.get_item_info.return_value = self.db.get_items_by_cookie.return_value[0]
        self.db.get_ai_reply_settings.return_value = {'max_discount_percent': 10, 'max_discount_amount': 20}
        def user(authorization: str = Header(default='')):
            if authorization != 'Bearer test': raise HTTPException(401, 'Unauthorized')
            return {'user_id': 1}
        app = FastAPI()
        app.include_router(create_commerce_router(self.db, user))
        app.include_router(create_order_tool_router(self.db))
        self.client = TestClient(app)
        patcher = patch('commerce_api.get_store', return_value=self.store)
        patcher.start(); self.addCleanup(patcher.stop)
        self.auth = {'Authorization': 'Bearer test'}

    def test_dashboard_authentication_and_account_ownership(self):
        self.assertEqual(self.client.get('/api/automation/products?account_id=seller').status_code, 401)
        self.assertEqual(self.client.get('/api/automation/products?account_id=other', headers=self.auth).status_code, 403)
        data = self.client.get('/api/automation/products?account_id=seller', headers=self.auth).json()
        self.assertEqual(data['items'][0]['automation']['content'], '')
        self.assertNotIn('cookie-not-returned', str(data))

    def test_product_configuration_persists_and_rejects_blank_delivery(self):
        path = '/api/automation/products/seller/item'
        self.assertEqual(self.client.put(path, headers=self.auth, json={'enabled': True, 'content': ''}).status_code, 422)
        self.assertEqual(self.client.put(path, headers=self.auth, json={'enabled': True, 'content': '网盘地址', 'min_price': '90', 'bargain_enabled': True}).status_code, 200)
        data = self.client.get('/api/automation/products?account_id=seller', headers=self.auth).json()
        self.assertEqual(data['items'][0]['automation']['content'], '网盘地址')
        self.db.get_item_info.return_value = None
        self.assertEqual(self.client.put(path, headers=self.auth, json={}).status_code, 404)

    def agreement(self):
        event = self.store.record_trade('seller', dict(order_id='order', item_id='item', buyer_id='buyer',
                                                      conversation_id='chat', status='pending'))
        self.store.save_config('seller', 'item', False, '', True, '90')
        seller = self.store.record_message('seller', 'chat', 'buyer', 'item', 'seller', '90元可以')
        buyer = self.store.record_message('seller', 'chat', 'buyer', 'item', 'buyer', '好的，按90元成交')
        return {'X-Xianyu-Order-Token': event['token']}, {
            'orderId': 'order', 'newPrice': '90', 'sellerMessageId': seller, 'buyerMessageId': buyer}

    def test_tool_requires_capability_and_cannot_change_another_order(self):
        headers, body = self.agreement()
        self.assertEqual(self.client.post('/api/bridge/change-order-price', json=body).status_code, 401)
        self.assertEqual(self.client.get('/api/bridge/negotiation-history', headers=headers).status_code, 200)
        body['orderId'] = 'other-order'
        self.assertEqual(self.client.post('/api/bridge/change-order-price', headers=headers, json=body).status_code, 403)

    def test_price_change_runs_adapter_and_updates_only_on_confirmed_success(self):
        headers, body = self.agreement()
        adapter = Mock(change=AsyncMock(return_value={'success': True, 'price': '90'}))
        live = Mock(cookies_str='snapshot')
        async def run(instance, operation, **kwargs): return await operation()
        with patch('bridge_api._get_instance', return_value=live), patch('bridge_api.run_on_instance', side_effect=run), \
             patch('utils.order_price_changer.OrderPriceChanger', return_value=adapter):
            result = self.client.post('/api/bridge/change-order-price', headers=headers, json=body)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertTrue(result.json()['ok'])
            self.db.insert_or_update_order.assert_called_once_with('order', amount='90')
            self.assertEqual(self.client.post('/api/bridge/change-order-price', headers=headers, json=body).status_code, 409)

    def test_price_change_is_rejected_after_payment_or_below_floor(self):
        headers, body = self.agreement()
        body['newPrice'] = '1'
        self.assertEqual(self.client.post('/api/bridge/change-order-price', headers=headers, json=body).status_code, 409)
        body['newPrice'] = '90'
        self.store.record_trade('seller', dict(order_id='order', item_id='item', buyer_id='buyer', conversation_id='chat', status='paid'))
        self.assertEqual(self.client.post('/api/bridge/change-order-price', headers=headers, json=body).status_code, 409)


if __name__ == '__main__': unittest.main()
