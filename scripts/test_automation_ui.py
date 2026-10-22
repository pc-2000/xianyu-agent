"""Browser smoke test against real automation routes and a temporary fixture DB."""
import asyncio
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright
import uvicorn
import commerce_api
import bridge_api
from utils.commerce_automation import CommerceStore


async def main():
    with tempfile.TemporaryDirectory() as temp:
        store = CommerceStore(Path(temp) / 'test.db')
        commerce_api.get_store = lambda: store
        sys.modules['XianyuAutoAsync'] = SimpleNamespace(XianyuLive=Mock())
        db = Mock()
        db.get_all_cookies.return_value = {'seller-a': 'not-exposed', 'seller-b': 'not-exposed'}
        def items(account):
            return [{'cookie_id': account, 'item_id': f'{account}-item', 'item_title': f'考研资料 · {account}', 'item_price': '100'}]
        db.get_items_by_cookie.side_effect = items
        db.get_item_info.side_effect = lambda account, item: next((i for i in items(account) if i['item_id'] == item), None)
        db.get_cookie_by_id.return_value = {'cookies_str': 'fixture'}
        def auth(authorization: str = Header(default='')):
            if authorization != 'Bearer ui-test': raise HTTPException(401)
            return {'user_id': 1}
        app = FastAPI()
        app.include_router(commerce_api.create_commerce_router(db, auth))
        @app.get('/verify')
        def verify(): return {'authenticated': True}
        @app.get('/cookies/details')
        def accounts(): return [{'id': a, 'remark': f'测试账号 {a}', 'enabled': True} for a in db.get_all_cookies()]
        @app.get('/')
        def index(): return FileResponse('static/index.html')
        app.mount('/static', StaticFiles(directory='static'), name='static')
        # Real sync route, fake account service; no external requests or mutations.
        for account in ('seller-a', 'seller-b'):
            bridge_api.xianyu_instances[account] = SimpleNamespace(_bridge_loop=None,
                get_all_items=AsyncMock(return_value={'success': True, 'total_count': 1, 'total_saved': 1,
                                                     'items': [{'id': f'{account}-item'}]}))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
        thread = threading.Thread(target=server.run, daemon=True); thread.start()
        for _ in range(100):
            if server.started: break
            await asyncio.sleep(.1)
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
                await page.add_init_script("localStorage.setItem('auth_token', 'ui-test')")
                await page.goto(f'http://127.0.0.1:{port}/')
                await page.get_by_role('button', name='自动发货', exact=True).click()
                await page.get_by_text('考研资料 · seller-a', exact=True).wait_for()
                await page.get_by_role('button', name='同步账号商品', exact=True).click()
                await page.get_by_text('同步完成，获取 1 件商品。已有交付配置保留。', exact=True).wait_for()
                await page.get_by_role('button', name='配置', exact=True).click()
                await page.get_by_label('付款后自动交付并确认无需物流发货').check()
                await page.get_by_label('交付内容', exact=True).fill('https://pan.example/kaoyan 提取码 TEST')
                await page.get_by_label('允许 Agent 按已谈妥价格修改未付款订单').check()
                await page.get_by_label('最低成交价', exact=True).fill('90')
                await page.get_by_role('button', name='保存配置', exact=True).click()
                await page.get_by_text('最低 ¥90', exact=True).wait_for()
                assert store.config('seller-a', 'seller-a-item')['content'].startswith('https://pan.example/')
                await page.get_by_label('卖家账号').select_option('seller-b')
                await page.get_by_text('考研资料 · seller-b', exact=True).wait_for()
                assert not store.config('seller-b', 'seller-b-item')['enabled']
                await page.get_by_label('卖家账号').select_option('seller-a')
                await page.get_by_text('最低 ¥90', exact=True).wait_for()
                await page.reload()
                await page.get_by_role('button', name='自动发货', exact=True).click()
                await page.get_by_text('最低 ¥90', exact=True).wait_for()
                output = Path('docs/screenshots'); output.mkdir(parents=True, exist_ok=True)
                await page.wait_for_timeout(700)
                await page.screenshot(path=str(output / 'auto-delivery.png'), full_page=True)
                await browser.close()
                print('UI PASS: account sync, save delivery/bargain config, account isolation, reload persistence')
        finally:
            server.should_exit = True
            thread.join(timeout=5)


if __name__ == '__main__': asyncio.run(main())
