"""Use the authenticated seller order UI; never guess an undocumented price API."""
import asyncio
import json
import re

from utils.commerce_automation import money


class OrderPriceChanger:
    def __init__(self, cookies_str):
        self.cookies_str = cookies_str

    async def change(self, order_id, new_price, validate):
        from playwright.async_api import async_playwright
        from utils.xianyu_utils import trans_cookies
        target = money(new_price)
        details = []
        submitted = False

        async def capture(response):
            if 'mtop.idle.web.trade.order.detail' in response.url:
                try:
                    result = await response.json()
                    if any(str(r).startswith('SUCCESS') for r in result.get('ret', [])):
                        details.append(result.get('data', {}))
                except Exception:
                    pass

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36')
                await context.add_cookies([{'name': k, 'value': v, 'domain': '.goofish.com', 'path': '/'}
                                          for k, v in trans_cookies(self.cookies_str).items()])
                page = await context.new_page()
                page.on('response', capture)
                await page.goto(f'https://www.goofish.com/order-detail?orderId={order_id}&role=seller',
                                wait_until='domcontentloaded', timeout=30000)
                change_button = page.get_by_text('修改价格', exact=True)
                await change_button.wait_for(state='visible', timeout=15000)
                await page.wait_for_timeout(500)
                current = extract_order_price(details[-1]) if details else None
                if current is None:
                    return {'success': False, 'submitted': False, 'error': '无法从订单详情取得可校验的待付款金额，未执行改价'}
                if money(current) == target:
                    return {'success': True, 'order_id': order_id, 'price': str(target), 'already_matched': True}
                validate(current)
                await change_button.click()
                await page.wait_for_timeout(400)
                if await page.get_by_text('修改价格待上线', exact=False).is_visible():
                    return {'success': False, 'submitted': False, 'code': 'unsupported_web_price_change',
                            'error': '闲鱼网页版显示“修改价格待上线，扫码去APP查看”，目前没有可提交的金额表单。请在闲鱼App改价，或接入经过验证的订单改价接口。'}
                dialog = page.get_by_role('dialog')
                try:
                    await dialog.wait_for(state='visible', timeout=3000)
                    scope = dialog
                except Exception:
                    scope = page
                field = scope.get_by_label(re.compile('转让价|商品金额|商品价格'))
                try:
                    await field.wait_for(state='visible', timeout=3000)
                except Exception:
                    field = scope.get_by_placeholder(re.compile('请输入.*价格|请输入.*金额|转让价'))
                    await field.wait_for(state='visible', timeout=3000)
                if await field.count() != 1:
                    return {'success': False, 'submitted': False, 'error': '订单改价页面没有唯一明确的金额输入框，未提交'}
                await field.fill(str(target))
                submit = scope.get_by_role('button', name=re.compile('^(确定|确认修改|确认|保存)$'))
                if await submit.count() != 1:
                    return {'success': False, 'submitted': False, 'error': '无法唯一定位确认改价按钮，未提交'}
                # Revalidate against the latest paid event immediately before mutation.
                validate(current)
                submitted = True
                await submit.click()
                await page.wait_for_timeout(1500)
                details.clear()
                await page.reload(wait_until='domcontentloaded', timeout=30000)
                for _ in range(20):
                    if details:
                        break
                    await asyncio.sleep(.25)
                verified = extract_order_price(details[-1]) if details else None
                if verified is not None and money(verified) == target:
                    return {'success': True, 'order_id': order_id, 'price': str(target)}
                return {'success': False, 'submitted': True, 'error': '已提交改价，但重新查询未确认目标金额；请人工核实，勿重复提交'}
            except Exception as exc:
                # Do not expose cookie-bearing browser traces or raw HTML to the agent.
                if not submitted:
                    try:
                        if await page.get_by_text('修改价格待上线', exact=False).is_visible():
                            return {'success': False, 'submitted': False, 'code': 'unsupported_web_price_change',
                                    'error': '闲鱼网页版暂未上线改价功能，请使用App改价或接入可用的订单改价接口。'}
                    except Exception:
                        pass
                return {'success': False, 'submitted': submitted, 'error': f'订单改价未完成：{type(exc).__name__}；请检查订单页是否支持修改价格'}
            finally:
                await browser.close()


def extract_order_price(data):
    """Only explicit unpaid status and a single-item order can be repriced."""
    status = data.get('utArgs', {}).get('orderStatusName', '')
    if not any(word in status for word in ('待付款', '等待买家付款', '等待付款', '还没付款', '尚未付款')):
        return None
    for component in data.get('components', []):
        if component.get('render') == 'orderInfoVO':
            info = component.get('data', {})
            item = info.get('itemInfo', {})
            if str(item.get('quantity', item.get('buyQuantity', 1))) != '1':
                return None
            value = info.get('priceInfo', {}).get('amount', {}).get('value')
            if value is not None:
                return str(money(value))
    return None
