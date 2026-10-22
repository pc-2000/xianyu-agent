import type { OpenClawPluginApi } from 'openclaw/plugin-sdk/core';
import { LRUCache } from 'lru-cache';

type Binding = { apiUrl: string; authorization: string; orderId: string };
// Capabilities stay outside model-visible text. Only the originating session can use them.
const bindings = new LRUCache<string, Binding>({ max: 1000, ttl: 3600000 });

export function bindOrderTools(sessionKey: string, value: Binding): void {
  bindings.set(sessionKey, { ...value });
}

export function registerOrderTools(api: OpenClawPluginApi): void {
  api.registerTool((context) => {
    const sessionKey = context.sessionKey;
    const request = async (path: string, body?: Record<string, unknown>) => {
      const binding = sessionKey ? bindings.get(sessionKey) : undefined;
      if (!binding) throw new Error('本会话没有有效的待付款下单事件，不能操作其他会话订单');
      if (body && body.orderId !== binding.orderId) throw new Error('订单号与当前下单事件不一致');
      const response = await fetch(`${binding.apiUrl}/api/bridge/${path}`, {
        method: body ? 'POST' : 'GET',
        headers: { 'Content-Type': 'application/json', 'X-Xianyu-Order-Token': binding.authorization },
        ...(body ? { body: JSON.stringify(body) } : {}),
        signal: AbortSignal.timeout(125000),
      });
      const result = await response.json() as Record<string, unknown>;
      if (!response.ok || result.ok === false) throw new Error(String(result.detail || result.error || '订单操作失败'));
      return { content: [{ type: 'text' as const, text: JSON.stringify(result) }], details: {} };
    };
    const safe = async (operation: () => ReturnType<typeof request>) => {
      try { return await operation(); }
      catch (error) { return { content: [{ type: 'text' as const, text: String(error) }], isError: true, details: {} }; }
    };
    return [
      {
        name: 'xianyu_get_negotiation_history', label: '查询本订单议价记录',
        description: '读取当前买家、当前会话的真实聊天记录及消息ID，用于核对是否已经谈妥价格。不能读取其他买家。',
        parameters: { type: 'object' as const, properties: {}, additionalProperties: false },
        execute: async () => safe(() => request('negotiation-history')),
      },
      {
        name: 'xianyu_change_order_price', label: '修改待付款订单价格',
        description: '仅在当前买家下单未付款、双方明确谈妥价格且符合卖家底价时调用。必须提供真实卖家报价和买家同意的消息ID；成功返回后才能告知改价成功。',
        parameters: {
          type: 'object' as const, additionalProperties: false,
          properties: {
            orderId: { type: 'string', description: '当前下单事件的订单号' },
            newPrice: { type: 'string', description: '谈妥的订单金额，单位元，例如110.00' },
            sellerMessageId: { type: 'string', description: '聊天记录中包含该金额的卖家报价消息ID' },
            buyerMessageId: { type: 'string', description: '聊天记录中买家明确同意该报价的消息ID' },
          },
          required: ['orderId', 'newPrice', 'sellerMessageId', 'buyerMessageId'],
        },
        execute: async (_id: string, params: unknown) => safe(() => request('change-order-price', params as Record<string, unknown>)),
      },
    ];
  }, { names: ['xianyu_get_negotiation_history', 'xianyu_change_order_price'] });
}
