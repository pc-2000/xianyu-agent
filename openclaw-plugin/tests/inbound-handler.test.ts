import { afterEach, expect, it, vi } from 'vitest';
import { handleBridgeMessage } from '../src/inbound-handler';
import { setXianyuRuntime } from '../src/runtime';
import { listXianyuAccountIds } from '../src/config';
import { readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

// Exercise the installed SDK's actual normalizer without booting the full Agent runtime.
const sdkDist = resolve(import.meta.dirname, '../node_modules/openclaw/dist');
const contextModule = readdirSync(sdkDist).find(name => /^inbound-context-.*\.js$/.test(name));
if (!contextModule) throw new Error('OpenClaw inbound context implementation not found');
const { n: finalizeInboundContext } = await import(pathToFileURL(resolve(sdkDist, contextModule)).href);

vi.mock('openclaw/plugin-sdk/channel-inbound', () => ({
  formatInboundEnvelope: (params: any) => params.body,
}));

afterEach(() => vi.unstubAllGlobals());

it('passes context to the Agent and posts only its final answer to the original buyer', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true })));
  vi.stubGlobal('fetch', fetchMock);
  const dispatch = vi.fn(async ({ ctx, dispatcherOptions }: any) => {
    expect(ctx.Body).toContain('bag');
    expect(ctx.Body).toContain('be concise');
    expect(ctx.BodyForAgent).toContain('be concise');
    expect(ctx.BodyForAgent).toContain('bag');
    expect(ctx.BodyForAgent).toContain('Available?');
    expect(ctx.agentText).toBe(ctx.BodyForAgent);
    expect(ctx.RawBody).toBe('Available?');
    expect(ctx.CommandBody).toBe('Available?');
    await dispatcherOptions.deliver({ text: 'working' }, { kind: 'block' });
    expect(fetchMock).not.toHaveBeenCalled();
    await dispatcherOptions.deliver({ text: 'Yes, available.' }, { kind: 'final' });
  });
  setXianyuRuntime({ channel: {
    routing: { resolveAgentRoute: () => ({ agentId: 'main', sessionKey: 'session', mainSessionKey: 'main' }) },
    session: { resolveStorePath: () => 'store', readSessionUpdatedAt: () => undefined, recordInboundSession: vi.fn() },
    reply: { resolveEnvelopeFormatOptions: () => ({}), finalizeInboundContext,
      dispatchReplyWithBufferedBlockDispatcher: dispatch },
  } } as any);
  await handleBridgeMessage({
    cfg: { session: { dmScope: 'per-account-channel-peer' }, channels: { xianyu: {
      enabled: true, apiUrl: 'http://bridge.test', accounts: { seller: { apiUrl: 'http://bridge.test' } },
    } } } as any,
    accountId: 'seller',
    data: { messageId: 'one', accountId: 'seller', conversationId: 'chat', senderId: 'buyer',
      senderName: 'Buyer', content: 'Available?', contentType: 'text', timestamp: Date.now(),
      replyContext: { product: { title: 'bag', price: '50', description: 'new' }, sellerInstructions: 'be concise',
        maxDiscountAmount: 5, maxDiscountPercent: 10, maxBargainRounds: 3 } },
  });
  expect(dispatch).toHaveBeenCalledOnce();
  expect(fetchMock).toHaveBeenCalledOnce();
  expect(fetchMock.mock.calls[0][0]).toBe('http://bridge.test/api/bridge/send');
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
    accountId: 'seller', conversationId: 'chat', toUserId: 'buyer', text: 'Yes, available.',
  });
});

it('does not start a second default subscription when named accounts exist', () => {
  expect(listXianyuAccountIds({ channels: { xianyu: {
    apiUrl: 'http://bridge.test', accounts: { seller: { apiUrl: 'http://bridge.test' } },
  } } } as any)).toEqual(['seller']);
});

it('routes an unpaid trade event into the buyer session without exposing its order capability', async () => {
  const dispatch = vi.fn(async ({ ctx }: any) => {
    expect(ctx.BodyForAgent).toContain('平台待付款订单事件');
    expect(ctx.BodyForAgent).toContain('order-123');
    expect(ctx.BodyForAgent).not.toContain('private-order-capability');
  });
  const route = vi.fn(() => ({ agentId: 'main', sessionKey: 'trade-session', mainSessionKey: 'main' }));
  setXianyuRuntime({ channel: {
    routing: { resolveAgentRoute: route },
    session: { resolveStorePath: () => 'store', readSessionUpdatedAt: () => undefined, recordInboundSession: vi.fn() },
    reply: { resolveEnvelopeFormatOptions: () => ({}), finalizeInboundContext,
      dispatchReplyWithBufferedBlockDispatcher: dispatch },
  } } as any);
  await handleBridgeMessage({ cfg: { session: { dmScope: 'per-account-channel-peer' },
    channels: { xianyu: { apiUrl: 'http://bridge.test', accounts: { seller: { apiUrl: 'http://bridge.test' } } } } } as any,
    accountId: 'seller', data: { messageId: 'order-event', accountId: 'seller', conversationId: 'chat',
      senderId: 'buyer', senderName: 'Buyer', contentType: 'trade', timestamp: Date.now(),
      content: '用户已经下单但尚未付款，请核对议价',
      orderContext: { orderId: 'order-123', status: 'pending', authorization: 'private-order-capability' },
      replyContext: { product: { title: '资料', price: '100', description: '' }, sellerInstructions: '',
        maxDiscountPercent: 10, maxDiscountAmount: 20, maxBargainRounds: 3,
        order: { orderId: 'order-123', itemId: 'item', status: 'pending', repricingAllowed: true, minimumPrice: '90' } },
    } });
  expect(route).toHaveBeenCalledWith(expect.objectContaining({ accountId: 'seller', peer: { kind: 'direct', id: 'chat' } }));
  expect(dispatch).toHaveBeenCalledOnce();
});
