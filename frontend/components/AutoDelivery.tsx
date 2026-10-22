import React, { useEffect, useState } from 'react';
import { PackageCheck, RefreshCw, Save, X, Loader2, Search, Settings2 } from 'lucide-react';
import { getAccountDetails, getAutomationProducts, saveProductAutomation, syncAutomationProducts,
  getAutomationDeliveries, retryDeliveryConfirmation, ProductAutomation, AutomationProduct, DeliveryRecord } from '../services/api';
import type { AccountDetail } from '../types';

const states: Record<string, string> = {
  ready: '等待发送', sending: '发送中', sent: '内容已发送', confirming: '正在确认发货',
  completed: '已完成发货', confirm_failed: '确认发货失败', send_uncertain: '发送结果待核实',
  legacy_sent: '旧版本已发送',
};

export default function AutoDelivery() {
  const [accounts, setAccounts] = useState<AccountDetail[]>([]);
  const [account, setAccount] = useState('');
  const [items, setItems] = useState<AutomationProduct[]>([]);
  const [records, setRecords] = useState<DeliveryRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [search, setSearch] = useState('');
  const [editing, setEditing] = useState<AutomationProduct | null>(null);
  const [draft, setDraft] = useState<ProductAutomation | null>(null);
  const [saving, setSaving] = useState(false);
  const [retrying, setRetrying] = useState('');

  useEffect(() => {
    getAccountDetails().then(data => { setAccounts(data); setAccount(data[0]?.id || ''); })
      .catch(e => setError(e.message));
  }, []);

  useEffect(() => {
    let cancelled = false;
    setItems([]); setRecords([]); setEditing(null); setDraft(null); setError(''); setNotice('');
    if (!account) return;
    setLoading(true);
    Promise.all([getAutomationProducts(account), getAutomationDeliveries(account)])
      .then(([products, deliveries]) => { if (!cancelled) { setItems(products.items); setRecords(deliveries.records); } })
      .catch(e => { if (!cancelled) setError(e.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [account]);

  const refresh = async () => {
    setError(''); setLoading(true);
    try {
      const [products, deliveries] = await Promise.all([getAutomationProducts(account), getAutomationDeliveries(account)]);
      setItems(products.items); setRecords(deliveries.records);
    } catch (e: any) { setError(e.message); }
    finally { setLoading(false); }
  };

  const sync = async () => {
    setSyncing(true); setError(''); setNotice('');
    try {
      const result = await syncAutomationProducts(account);
      await refresh();
      setNotice(`同步完成，获取 ${result.total} 件商品。已有交付配置保留。`);
    } catch (e: any) { setError(e.message); }
    finally { setSyncing(false); }
  };

  const save = async () => {
    if (!editing || !draft) return;
    setSaving(true); setError('');
    try {
      await saveProductAutomation(account, String(editing.item_id), draft);
      setEditing(null); setDraft(null);
      await refresh(); setNotice('商品配置已保存，下次收到该商品付款通知时生效。');
    } catch (e: any) { setError(e.message); }
    finally { setSaving(false); }
  };

  const retry = async (order: string) => {
    setRetrying(order); setError('');
    try { await retryDeliveryConfirmation(account, order); await refresh(); }
    catch (e: any) { setError(e.message); }
    finally { setRetrying(''); }
  };

  const filtered = items.filter(item => `${item.item_title || ''} ${item.item_id}`.includes(search));
  return <div className="space-y-6 animate-fade-in">
    <div className="flex flex-wrap items-center justify-between gap-4">
      <div><h2 className="text-3xl font-bold text-gray-900">自动发货</h2>
        <p className="text-gray-500 text-sm mt-2">按卖家账号配置商品，买家付款后发送交付内容并确认无需物流发货。</p></div>
      <div className="flex flex-wrap gap-3">
        <select aria-label="卖家账号" className="ios-input px-4 py-3 rounded-xl text-sm" value={account}
          disabled={syncing || saving || !!retrying} onChange={e => setAccount(e.target.value)}>
          {!accounts.length && <option value="">请先在账号管理添加账号</option>}
          {accounts.map(a => <option key={a.id} value={a.id}>{a.remark || a.nickname || a.id} · {a.id}</option>)}
        </select>
        <button onClick={sync} disabled={!account || syncing || loading} className="ios-btn-primary rounded-xl px-5 py-3 flex items-center gap-2 disabled:opacity-50">
          <RefreshCw className={`w-4 h-4 ${syncing ? 'animate-spin' : ''}`} />{syncing ? '同步中…' : '同步账号商品'}</button>
      </div>
    </div>
    {error && <div role="alert" className="bg-red-50 text-red-700 p-4 rounded-2xl">{error}</div>}
    {notice && <div role="status" className="bg-green-50 text-green-800 p-4 rounded-2xl">{notice}</div>}
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
      {[['已同步商品', items.length], ['已启用自动发货', items.filter(i => i.automation.enabled).length],
        ['最近完成交付', records.filter(r => r.status === 'completed').length]].map(([label, count]) =>
        <div key={String(label)} className="bg-white rounded-2xl p-5 border border-gray-100"><p className="text-sm text-gray-500">{label}</p><p className="text-3xl font-bold mt-2">{count}</p></div>)}
    </div>
    <div className="bg-white rounded-3xl border border-gray-100 overflow-hidden">
      <div className="p-5 flex justify-between items-center gap-3 border-b border-gray-100">
        <h3 className="font-bold flex items-center gap-2"><PackageCheck className="w-5 h-5" />商品交付配置</h3>
        <div className="relative"><Search className="absolute left-3 top-3 w-4 h-4 text-gray-400" />
          <input aria-label="搜索商品" value={search} onChange={e => setSearch(e.target.value)} placeholder="商品名称或ID" className="ios-input pl-9 pr-3 py-2 rounded-xl text-sm" /></div>
      </div>
      {loading ? <div className="p-12 flex justify-center"><Loader2 className="animate-spin" /></div> :
        <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="bg-gray-50 text-gray-500"><tr>
          <th className="p-4">商品</th><th className="p-4">价格</th><th className="p-4">自动发货</th><th className="p-4">自动改价</th><th className="p-4">操作</th>
        </tr></thead><tbody>{filtered.map(item => <tr key={String(item.item_id)} className="border-t border-gray-50">
          <td className="p-4 max-w-sm"><div className="font-medium">{item.item_title || '未命名商品'}</div><div className="text-xs text-gray-400 mt-1">{item.item_id}</div></td>
          <td className="p-4">¥{item.item_price || '—'}</td>
          <td className="p-4"><span className={item.automation.enabled ? 'text-green-700' : 'text-gray-400'}>{item.automation.enabled ? '已启用' : '未启用'}</span></td>
          <td className="p-4">{item.automation.bargain_enabled ? `最低 ¥${item.automation.min_price}` : '未启用'}</td>
          <td className="p-4"><button className="flex items-center gap-1 font-bold hover:text-yellow-700" onClick={() => { setEditing(item); setDraft({ ...item.automation }); setError(''); }}><Settings2 className="w-4 h-4" />配置</button></td>
        </tr>)}</tbody></table>{!filtered.length && <p className="p-12 text-center text-gray-400">暂无商品，请选择账号并同步商品。</p>}</div>}
    </div>
    <div className="bg-white rounded-3xl border border-gray-100 overflow-hidden">
      <div className="p-5 border-b border-gray-100 flex justify-between"><h3 className="font-bold">最近交付记录</h3><button onClick={refresh} disabled={!account || loading} className="text-sm text-gray-500">刷新记录</button></div>
      <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="bg-gray-50 text-gray-500"><tr><th className="p-4">订单号</th><th className="p-4">商品ID</th><th className="p-4">状态</th><th className="p-4">说明 / 操作</th></tr></thead>
        <tbody>{records.map(r => <tr key={r.order_id} className="border-t border-gray-50"><td className="p-4 font-mono text-xs">{r.order_id}</td><td className="p-4 text-xs">{r.item_id}</td><td className="p-4">{states[r.status] || r.status}</td><td className="p-4 text-gray-500 max-w-md">{r.error}
          {['confirm_failed', 'sent'].includes(r.status) && <button onClick={() => retry(r.order_id)} disabled={!!retrying} className="ml-3 font-bold text-yellow-800">{retrying === r.order_id ? '确认中…' : '重试确认发货'}</button>}</td></tr>)}</tbody></table>
        {!records.length && <p className="p-8 text-center text-gray-400">付款后会在这里显示内容发送与确认发货结果。</p>}</div>
    </div>
    {editing && draft && <div className="fixed inset-0 z-50 bg-black/30 flex items-center justify-center p-5" role="dialog" aria-modal="true" aria-label="商品自动化配置">
      <div className="bg-white rounded-3xl p-7 max-w-2xl w-full max-h-[90vh] overflow-y-auto space-y-5">
        <div className="flex justify-between items-start"><div><h3 className="text-xl font-bold">商品交付配置</h3><p className="text-sm text-gray-500 mt-1">{editing.item_title} · {editing.item_id}</p></div><button aria-label="关闭配置" disabled={saving} onClick={() => { setEditing(null); setDraft(null); }}><X /></button></div>
        {error && <div role="alert" className="bg-red-50 text-red-700 p-3 rounded-xl text-sm">{error}</div>}
        <label className="flex gap-3 items-center font-medium"><input type="checkbox" checked={!!draft.enabled} onChange={e => setDraft({ ...draft, enabled: e.target.checked })} />付款后自动交付并确认无需物流发货</label>
        <label className="block text-sm font-medium">交付内容<textarea aria-label="交付内容" className="ios-input block w-full rounded-xl p-4 mt-2 min-h-36" placeholder="例如：网盘地址、提取码、使用说明。只有付款成功后才会发送。" maxLength={10000} value={draft.content} onChange={e => setDraft({ ...draft, content: e.target.value })} /></label>
        <p className="text-xs text-gray-500">未配置或未启用的商品不会发送任何默认账号密码。下单未付款不会触发交付。</p>
        <div className="border-t border-gray-100 pt-5 space-y-3">
          <label className="flex gap-3 items-center font-medium"><input type="checkbox" checked={!!draft.bargain_enabled} onChange={e => setDraft({ ...draft, bargain_enabled: e.target.checked })} />允许 Agent 按已谈妥价格修改未付款订单</label>
          <label className="block text-sm">最低成交价（元）<input aria-label="最低成交价" type="number" min="0.01" step="0.01" value={draft.min_price} onChange={e => setDraft({ ...draft, min_price: e.target.value })} className="ios-input ml-3 p-3 rounded-xl w-36" /></label>
          <p className="text-xs text-gray-500">还会同时检查账号的最大优惠比例、金额及双方议价记录。已付款订单不能改价。</p>
        </div>
        <button onClick={save} disabled={saving} className="ios-btn-primary w-full py-3 rounded-xl flex justify-center items-center gap-2 disabled:opacity-50">{saving ? <Loader2 className="animate-spin w-4 h-4" /> : <Save className="w-4 h-4" />}保存配置</button>
      </div>
    </div>}
  </div>;
}
