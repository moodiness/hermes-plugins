import { host, ROUTES_AREA, SIDEBAR_NAV_AREA, Button, ConfirmDialog, EmptyState, ErrorState, LogView, StatusDot, useQuery } from '@hermes/plugin-sdk';
import React, { useState } from 'react';
import { jsx, jsxs } from 'react/jsx-runtime';

function OmpDashboard({ ctx }) {
  const [pending, setPending] = useState(null);
  const query = useQuery({
    queryKey: ['hermes-omp', 'snapshot'],
    queryFn: () => ctx.rest('/snapshot', { timeoutMs: 5000 }),
    refetchInterval: 5000,
  });
  if (query.isError) return jsx(ErrorState, { title: 'OMP dashboard unavailable', description: String(query.error) });
  if (!query.data) return jsx(EmptyState, { title: 'Loading OMP sessions' });
  const data = query.data;
  const rows = data.sessions.map((session) => jsxs('div', {
    className: 'flex items-center gap-2 border-b border-[var(--ui-stroke-secondary)] p-2',
    children: [jsx(StatusDot, { status: session.health === 'healthy' ? 'success' : 'neutral' }), jsx('strong', { children: session.name }), jsx('span', { className: 'text-[var(--ui-text-secondary)]', children: session.status }), jsx(Button, { variant: 'ghost', onClick: () => setPending({ action: 'restart', session: session.name }), children: 'Restart…' })],
  }, session.id));
  return jsxs('div', { className: 'p-3', children: [
    jsx('h2', { children: 'OMP sessions' }),
    jsx('p', { className: 'text-[var(--ui-text-secondary)]', children: 'Read-only by default. Questions, health, and bounded redacted local logs refresh every five seconds.' }),
    ...rows,
    jsx('h3', { children: `Questions (${data.questions.length})` }),
    jsx('pre', { children: JSON.stringify(data.questions, null, 2) }),
    jsx(LogView, { entries: data.logs }),
    jsx(ConfirmDialog, { open: Boolean(pending), title: 'Confirm OMP action', description: pending ? `${pending.action} ${pending.session}?` : '', onOpenChange: (open) => { if (!open) setPending(null); }, onConfirm: async () => { await ctx.rest('/action', { method: 'POST', body: { ...pending, confirmation: true } }); host.notify({ kind: 'info', message: 'Validated safe CLI contract; execute it explicitly in the CLI.' }); setPending(null); } }),
  ] });
}

export default {
  id: 'omp',
  name: 'OMP Dashboard',
  defaultEnabled: false,
  register(ctx) {
    ctx.register({ id: 'omp-route', area: ROUTES_AREA, data: { path: '/omp' }, render: () => jsx(OmpDashboard, { ctx }) });
    ctx.register({ id: 'omp-nav', area: SIDEBAR_NAV_AREA, data: { path: '/omp', label: 'OMP', codicon: 'server-process' } });
  },
};
