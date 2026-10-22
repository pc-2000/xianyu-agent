import { expect, it, vi, afterEach } from 'vitest';
import { bindOrderTools, registerOrderTools } from '../src/order-tools';

afterEach(() => vi.unstubAllGlobals());

function tools(sessionKey: string) {
  const registerTool = vi.fn();
  registerOrderTools({ registerTool } as any);
  return registerTool.mock.calls[0][0]({ sessionKey });
}

it('does not expose an order token or allow a different session to operate', async () => {
  bindOrderTools('session-owner', { apiUrl: 'http://bridge.test', authorization: 'secret-token', orderId: 'order' });
  const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
  const tool = tools('other-session').find((t: any) => t.name === 'xianyu_change_order_price');
  const result = await tool.execute('id', { orderId: 'order', newPrice: '90', sellerMessageId: 's', buyerMessageId: 'b' });
  expect(result.isError).toBe(true); expect(fetch).not.toHaveBeenCalled();
  expect(JSON.stringify(tool.parameters)).not.toContain('secret-token');
});

it('binds a price change to the trusted order and sends capability outside arguments', async () => {
  bindOrderTools('price-session', { apiUrl: 'http://bridge.test', authorization: 'secret-token', orderId: 'order' });
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true, success: true }) });
  vi.stubGlobal('fetch', fetch);
  const tool = tools('price-session').find((t: any) => t.name === 'xianyu_change_order_price');
  const args = { orderId: 'order', newPrice: '90', sellerMessageId: 'seller-id', buyerMessageId: 'buyer-id' };
  await tool.execute('id', args);
  expect(fetch.mock.calls[0][0]).toBe('http://bridge.test/api/bridge/change-order-price');
  expect(fetch.mock.calls[0][1].headers['X-Xianyu-Order-Token']).toBe('secret-token');
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(args);
  const bad = await tool.execute('id2', { ...args, orderId: 'another' });
  expect(bad.isError).toBe(true); expect(fetch).toHaveBeenCalledOnce();
});

it('surfaces backend refusal instead of claiming success', async () => {
  bindOrderTools('refused-session', { apiUrl: 'http://bridge.test', authorization: 'token', orderId: 'order' });
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, json: async () => ({ detail: '订单已经付款' }) }));
  const tool = tools('refused-session').find((t: any) => t.name === 'xianyu_get_negotiation_history');
  expect((await tool.execute()).isError).toBe(true);
});
