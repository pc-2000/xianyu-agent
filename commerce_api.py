"""Authenticated dashboard APIs and session-bound Agent order-price tool API."""
import asyncio
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from loguru import logger

from utils.commerce_automation import get_store


class AutomationConfig(BaseModel):
    enabled: bool = False
    content: str = Field(default='', max_length=10000)
    bargain_enabled: bool = False
    min_price: str = '0'


class PriceChange(BaseModel):
    orderId: str
    newPrice: str
    sellerMessageId: str
    buyerMessageId: str


def create_commerce_router(db, current_user):
    router = APIRouter(prefix='/api/automation', tags=['商品自动发货'])

    def owned(account, user):
        if account not in db.get_all_cookies(user['user_id']):
            raise HTTPException(403, '无权操作该卖家账号')

    @router.get('/products')
    def products(account_id: str, user=Depends(current_user)):
        owned(account_id, user)
        store = get_store()
        catalog = store.catalog(account_id)
        return {'items': [{**item, 'automation': store.config(account_id, str(item['item_id']))}
                          for item in db.get_items_by_cookie(account_id)
                          if catalog is None or str(item['item_id']) in catalog]}

    @router.put('/products/{account_id}/{item_id}')
    def save_product(account_id: str, item_id: str, body: AutomationConfig, user=Depends(current_user)):
        owned(account_id, user)
        if not db.get_item_info(account_id, item_id):
            raise HTTPException(404, '请先同步该账号的商品')
        try:
            return {'success': True, 'automation': get_store().save_config(account_id, item_id, **body.model_dump())}
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @router.post('/sync/{account_id}')
    async def sync_products(account_id: str, user=Depends(current_user)):
        owned(account_id, user)
        cookie = db.get_cookie_by_id(account_id)
        if not cookie:
            raise HTTPException(404, '账号不存在')
        from XianyuAutoAsync import XianyuLive
        from bridge_api import xianyu_instances, run_on_instance
        running = xianyu_instances.get(account_id)
        if running is not None:
            result = await run_on_instance(running, running.get_all_items, timeout=300)
        else:
            previous = XianyuLive.get_instance(account_id)
            live = XianyuLive(cookie['cookies_str'], account_id)
            try:
                result = await live.get_all_items()
            finally:
                await live.close_session()
                if XianyuLive.get_instance(account_id) is live:
                    live._unregister_instance()
                    if previous is not None:
                        previous._register_instance()
        if result.get('error') or not result.get('success'):
            raise HTTPException(502, '同步商品失败，请检查账号登录状态')
        get_store().save_catalog(account_id, [i['id'] for i in result.get('items', []) if i.get('id')])
        return {'success': True, 'total': result.get('total_count', 0), 'saved': result.get('total_saved', 0)}

    @router.get('/deliveries')
    def deliveries(account_id: str, user=Depends(current_user)):
        owned(account_id, user)
        return {'records': get_store().jobs([account_id])}

    @router.post('/deliveries/{account_id}/{order_id}/retry-confirm')
    async def retry_confirm(account_id: str, order_id: str, user=Depends(current_user)):
        owned(account_id, user)
        from bridge_api import _get_instance, run_on_instance
        store = get_store()
        event = store.event(account_id, order_id)
        records = {r['order_id']: r for r in store.jobs([account_id], 10000)}
        if not event or order_id not in records or records[order_id]['status'] not in ('confirm_failed', 'sent'):
            raise HTTPException(409, '此记录不允许重试确认；发送不确定时请先人工核实')
        live = _get_instance(account_id)
        async def operation():
            return await store.deliver(account_id, event,
                                       lambda text: live.send_msg(live.ws, event['conversation_id'], event['buyer_id'], text),
                                       lambda: live.auto_confirm(order_id, event['item_id']))
        return {'success': True, 'status': await run_on_instance(live, operation)}

    return router


def create_order_tool_router(db):
    router = APIRouter(prefix='/api/bridge', tags=['Agent订单工具'])

    def event_for(token):
        if not token:
            raise HTTPException(401, '需要由当前买家会话提供订单操作凭证')
        try:
            return get_store().authorize(token)
        except ValueError as exc:
            raise HTTPException(403, str(exc))

    @router.get('/negotiation-history')
    def negotiation_history(x_xianyu_order_token: str = Header(default='')):
        event = event_for(x_xianyu_order_token)
        return {'orderId': event['order_id'], 'messages': get_store().history(
            event['account_id'], event['conversation_id'], event['buyer_id'])}

    @router.post('/change-order-price')
    async def change_order_price(body: PriceChange, x_xianyu_order_token: str = Header(default='')):
        store = get_store()
        event = event_for(x_xianyu_order_token)
        if body.orderId != event['order_id']:
            raise HTTPException(403, '工具只能操作本会话下单事件中的订单')
        settings = db.get_ai_reply_settings(event['account_id'])
        item = db.get_item_info(event['account_id'], event['item_id']) or {}
        def validate(current_price):
            try:
                return store.validate_price(x_xianyu_order_token, body.newPrice, body.sellerMessageId,
                                            body.buyerMessageId, settings, current_price)
            except ValueError as exc:
                logger.warning(f'订单改价校验拒绝 account={event["account_id"]} order={event["order_id"]}: {exc}')
                raise HTTPException(409, str(exc))
        validate(item.get('item_price') or '0')
        from bridge_api import _get_instance, run_on_instance
        from utils.order_price_changer import OrderPriceChanger
        live = _get_instance(event['account_id'])
        if not store.claim_price(event['account_id'], event['order_id'], body.newPrice):
            raise HTTPException(409, '该订单已经执行或正在执行改价，请先查询结果，禁止重复提交')
        try:
            # The browser uses a cookie snapshot and runs on the account's owning loop.
            result = await run_on_instance(live, lambda: OrderPriceChanger(live.cookies_str).change(
                body.orderId, body.newPrice, validate), timeout=120)
        except Exception:
            store.finish_price(event['account_id'], event['order_id'], False)
            raise
        store.finish_price(event['account_id'], event['order_id'], result.get('success') is True,
                           retry_safe=result.get('submitted') is False)
        if result.get('success'):
            db.insert_or_update_order(body.orderId, amount=str(body.newPrice))
        return {'ok': result.get('success') is True, **result}

    return router
