"""Read-only inspection of seller order status/price UI; never submits changes."""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from order_status_query_playwright import OrderStatusQueryPlaywright
from utils.xianyu_utils import trans_cookies


async def main():
    order = sys.argv[1]
    with closing(sqlite3.connect('file:data/commerce_automation.db?mode=ro', uri=True)) as conn:
        row = conn.execute('SELECT account_id FROM trade_events WHERE order_id=?', (order,)).fetchone()
        if not row: raise ValueError('Order event not found')
        account = row[0]
    with closing(sqlite3.connect('file:data/xianyu_data.db?mode=ro', uri=True)) as conn:
        cookie = conn.execute('SELECT value FROM cookies WHERE id=?', (account,)).fetchone()[0]
    result = await OrderStatusQueryPlaywright(cookie, account).query_order_status(order)
    raw = result.get('raw_data', {})
    buttons = raw.get('bottomBarVO', {}).get('buttonList', [])
    if isinstance(buttons, dict): buttons = list(buttons.values())
    print(json.dumps({'success': result.get('success'), 'status': result.get('status_text'), 'statusCode': result.get('order_status'),
        'price': result.get('price'), 'data_keys': list(raw.keys()),
        'components': [c.get('render') for c in raw.get('components', [])],
        'buttons': [{k: v for k, v in button.items() if k in ('tradeAction', 'text', 'title', 'buttonText', 'name')}
                    for button in buttons if isinstance(button, dict)]}, ensure_ascii=False))
    if '--form' in sys.argv:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36')
                await context.add_cookies([{'name': k, 'value': v, 'domain': '.goofish.com', 'path': '/'}
                                          for k, v in trans_cookies(cookie).items()])
                page = await context.new_page()
                await page.goto(f'https://www.goofish.com/order-detail?orderId={order}&role=seller', wait_until='networkidle')
                button = page.get_by_text('修改价格', exact=True)
                try:
                    await button.wait_for(state='visible', timeout=10000)
                except Exception:
                    text = await page.locator('body').inner_text()
                    print('order page relevant text:', json.dumps([line for line in text.splitlines()
                        if any(word in line for word in ('价格', '改价', '付款', '支付', '扫码', '登录', '订单', '手机'))], ensure_ascii=False))
                    print('page buttons:', json.dumps(await page.get_by_role('button').all_text_contents(), ensure_ascii=False))
                    print('frame count:', len(page.frames))
                    output = Path('docs/screenshots'); output.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(output / 'order-price-inspection.png'), full_page=True)
                    return
                await button.click()  # Opens a form only; never fills or submits it.
                await page.wait_for_timeout(1200)
                print('visible form inputs:', json.dumps(await page.locator('input:visible').evaluate_all(
                    "els => els.map(e => ({type:e.type,placeholder:e.placeholder,ariaLabel:e.getAttribute('aria-label'),value:/^[0-9.]+$/.test(e.value)?e.value:'[omitted]',parentText:e.parentElement?.innerText?.slice(0,100)}))"), ensure_ascii=False))
                print('visible buttons:', json.dumps(await page.get_by_role('button').all_text_contents(), ensure_ascii=False))
                text = await page.locator('body').inner_text()
                print('form relevant text:', json.dumps([line for line in text.splitlines()
                    if any(word in line for word in ('价格', '改价', '扫码', '手机', 'APP', '暂不支持'))], ensure_ascii=False))
                output = Path('docs/screenshots'); output.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(output / 'order-price-form-inspection.png'), full_page=True)
            finally:
                await browser.close()


if __name__ == '__main__': asyncio.run(main())
