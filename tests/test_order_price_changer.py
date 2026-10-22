import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from utils.order_price_changer import OrderPriceChanger


class OrderPriceBrowserTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, initial='100', final='90', pending=True, unsupported=False):
        handler = {}
        def data(price):
            return {'ret': ['SUCCESS::调用成功'], 'data': {
                'utArgs': {'orderStatusName': '等待买家付款' if pending else '等待卖家发货'},
                'components': [{'render': 'orderInfoVO', 'data': {
                    'itemInfo': {'quantity': 1}, 'priceInfo': {'amount': {'value': price}}}}]}}
        async def emit(price):
            await handler['response'](SimpleNamespace(url='https://h5api.m.goofish.com/mtop.idle.web.trade.order.detail/',
                                                      json=AsyncMock(return_value=data(price))))
        field = Mock(count=AsyncMock(return_value=1), fill=AsyncMock(), wait_for=AsyncMock())
        submit = Mock(count=AsyncMock(return_value=1), click=AsyncMock())
        dialog = Mock(count=AsyncMock(return_value=1), wait_for=AsyncMock(), get_by_label=Mock(return_value=field), get_by_role=Mock(return_value=submit))
        change = Mock(wait_for=AsyncMock(), click=AsyncMock())
        unavailable = Mock(is_visible=AsyncMock(return_value=unsupported))
        page = Mock(goto=AsyncMock(side_effect=lambda *a, **kw: emit(initial)),
                    reload=AsyncMock(side_effect=lambda *a, **kw: emit(final)),
                    wait_for_timeout=AsyncMock(), get_by_text=Mock(side_effect=lambda text, **kw: change if text == '修改价格' else unavailable),
                    get_by_role=Mock(return_value=dialog))
        # AsyncMock does not await a coroutine returned by a synchronous side effect.
        async def goto(*args, **kwargs): await emit(initial)
        async def reload(*args, **kwargs): await emit(final)
        page.goto.side_effect = goto; page.reload.side_effect = reload
        page.on.side_effect = lambda name, callback: handler.update({name: callback})
        context = Mock(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page))
        browser = Mock(new_context=AsyncMock(return_value=context), close=AsyncMock())
        runtime = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        manager = SimpleNamespace()
        # Special methods must live on the class for async with.
        class Manager:
            async def __aenter__(self): return runtime
            async def __aexit__(self, *args): pass
        return Manager(), field, submit, browser

    async def test_submit_only_after_validation_and_verify_new_price(self):
        manager, field, submit, browser = self.fixture()
        validate = Mock()
        with patch('playwright.async_api.async_playwright', return_value=manager):
            result = await OrderPriceChanger('unb=seller').change('order', '90', validate)
        self.assertTrue(result['success']); self.assertEqual(validate.call_count, 2)
        field.fill.assert_awaited_once_with('90'); submit.click.assert_awaited_once(); browser.close.assert_awaited_once()

    async def test_paid_order_never_submits(self):
        manager, field, submit, _ = self.fixture(pending=False)
        with patch('playwright.async_api.async_playwright', return_value=manager):
            result = await OrderPriceChanger('unb=seller').change('order', '90', Mock())
        self.assertFalse(result['success']); field.fill.assert_not_awaited(); submit.click.assert_not_awaited()

    async def test_paid_event_during_browser_work_blocks_submission(self):
        manager, field, submit, _ = self.fixture()
        validate = Mock(side_effect=[None, ValueError('paid')])
        with patch('playwright.async_api.async_playwright', return_value=manager):
            result = await OrderPriceChanger('unb=seller').change('order', '90', validate)
        self.assertFalse(result['success']); submit.click.assert_not_awaited()

    async def test_unverified_change_does_not_report_success(self):
        manager, field, submit, _ = self.fixture(final='100')
        with patch('playwright.async_api.async_playwright', return_value=manager):
            result = await OrderPriceChanger('unb=seller').change('order', '90', Mock())
        self.assertFalse(result['success']); submit.click.assert_awaited_once()

    async def test_platform_app_only_notice_is_reported_without_submitting(self):
        manager, field, submit, _ = self.fixture(unsupported=True)
        with patch('playwright.async_api.async_playwright', return_value=manager):
            result = await OrderPriceChanger('unb=seller').change('order', '90', Mock())
        self.assertEqual(result['code'], 'unsupported_web_price_change')
        self.assertFalse(result['submitted']); field.fill.assert_not_awaited(); submit.click.assert_not_awaited()


if __name__ == '__main__': unittest.main()
