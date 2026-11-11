/* Club100 Wallet Desk overview — read-only, values supplied by server.
 * It deliberately does not save or change grants/ledger/balances.
 */
frappe.ui.form.on('Club100 Wallet', {
    refresh(frm) {
        if (frm.is_new()) return;
        frm.add_custom_button(__('Refresh Balance'), () => club100LoadWalletDashboard(frm));
        frm.add_custom_button(__('Credit Grants'), () => {
            frappe.set_route('List', 'Club100 Wallet Grant', {wallet: frm.doc.name});
        }, __('View'));
        frm.add_custom_button(__('Transactions'), () => {
            frappe.set_route('List', 'Club100 Wallet Ledger', {wallet: frm.doc.name});
        }, __('View'));
        club100LoadWalletDashboard(frm);
    }
});

function club100WalletEsc(value) {
    return String(value === null || value === undefined ? '' : value)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function club100WalletMoney(raw) {
    const amount = Number(raw || 0);
    if (!Number.isFinite(amount)) return club100WalletEsc(raw);
    return new Intl.NumberFormat('en-IN', {
        style: 'currency', currency: 'INR', minimumFractionDigits: 2, maximumFractionDigits: 2
    }).format(amount);
}

function club100WalletTable(headers, body, emptyText) {
    const heading = headers.map(h => `<th style="white-space:nowrap">${club100WalletEsc(h)}</th>`).join('');
    return `<div style="overflow-x:auto"><table class="table table-bordered table-sm" style="margin-bottom:8px">
      <thead><tr>${heading}</tr></thead><tbody>${body || `<tr><td colspan="${headers.length}" class="text-muted">${club100WalletEsc(emptyText)}</td></tr>`}</tbody>
      </table></div>`;
}

function club100WalletCard(label, amount, accent) {
    return `<div class="club100-w-card">
        <div class="text-muted" style="font-size:12px">${club100WalletEsc(label)}</div>
        <div style="font-size:21px;font-weight:650;color:${accent || 'inherit'}">${club100WalletMoney(amount)}</div>
      </div>`;
}

function club100WalletRender(summary) {
    const b = summary.balances;
    const grants = (summary.grants || []).map(g => `<tr>
      <td><code>${club100WalletEsc(g.grant)}</code></td>
      <td>${club100WalletEsc(g.source)}</td>
      <td class="text-right">${club100WalletMoney(g.original_amount)}</td>
      <td class="text-right">${club100WalletMoney(g.remaining)}</td>
      <td class="text-right">${club100WalletMoney(g.reserved)}</td>
      <td class="text-right"><strong>${club100WalletMoney(g.available)}</strong></td>
      <td>${club100WalletEsc(g.validity === 'Lifetime' ? 'Lifetime' : (g.expires_on || '—'))}</td>
      <td>${club100WalletEsc(g.state)}</td>
      <td>${club100WalletEsc(g.corporate_pool || '—')}</td>
    </tr>`).join('');

    const transactions = (summary.transactions || []).map(t => `<tr>
      <td>${club100WalletEsc(t.created_at)}</td>
      <td>${club100WalletEsc(t.type)}</td>
      <td class="text-right">${club100WalletMoney(t.amount)}</td>
      <td><code>${club100WalletEsc(t.grant)}</code></td>
      <td>${club100WalletEsc(t.reference || '—')}</td>
      <td><code>${club100WalletEsc(t.id)}</code></td>
    </tr>`).join('');

    const grantLimited = summary.grant_count > (summary.grants || []).length;
    const ledgerLimited = summary.transaction_count > (summary.transactions || []).length;
    const main = [
        club100WalletCard('Available to Spend', b.available),
        club100WalletCard('Currently Reserved', b.reserved),
        club100WalletCard('Expired • Unspent', b.expired_unspent),
        club100WalletCard('Future-dated', b.future_unvested)
    ].join('');
    const sources = ['Corporate', 'Referral', 'Promotion'].map(s =>
        club100WalletCard(s, (b.by_source || {})[s] || '0.00')
    ).join('');

    return `<div class="club100-wallet-dashboard">
      <style>
        .club100-wallet-dashboard{padding:10px 0 18px}
        .club100-wallet-dashboard .club100-w-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:10px;margin:12px 0}
        .club100-wallet-dashboard .club100-w-card{padding:14px;border:1px solid var(--border-color, #ddd);border-radius:8px;background:var(--card-bg, transparent)}
        .club100-wallet-dashboard .club100-w-section{margin-top:24px}
        .club100-wallet-dashboard .club100-w-section h5{margin:0 0 10px;font-weight:650}
        .club100-wallet-dashboard .table{font-size:12px}
        .club100-wallet-dashboard .table td{vertical-align:middle}
        .club100-wallet-dashboard .table code{font-size:11px}
      </style>
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
        <h4 style="margin:0">Consolidated Balance</h4>
        <span class="text-muted" style="font-size:12px">As of ${club100WalletEsc(summary.as_of)} • INR • ${club100WalletEsc(summary.status)}</span>
      </div>
      <div class="club100-w-grid">${main}</div>
      <h5 style="margin-top:18px">Available by Funding Source</h5>
      <div class="club100-w-grid">${sources}</div>
      <div class="text-muted" style="font-size:12px">Available balances exclude active checkout holds, expired credits and future-dated credits. Lifetime grants have no expiry.</div>
      <div class="club100-w-section">
        <h5>Credit Grants <small class="text-muted">(${summary.grant_count} active)</small></h5>
        ${club100WalletTable(['Grant','Source','Issued','Remaining','Reserved','Available','Expires','State','Corporate Pool'], grants, 'No active credit grants')}
        ${grantLimited ? `<div class="text-muted">Showing ${summary.grants.length} of ${summary.grant_count} active grants. Use View → Credit Grants to see all.</div>` : ''}
      </div>
      <div class="club100-w-section">
        <h5>Wallet Transactions <small class="text-muted">(${summary.transaction_count} total)</small></h5>
        ${club100WalletTable(['Recorded At','Type','Signed Amount','Grant','Reference','Ledger Event'], transactions, 'No wallet transactions')}
        ${ledgerLimited ? `<div class="text-muted">Showing latest ${summary.transactions.length} of ${summary.transaction_count} transactions. Use View → Transactions to see all.</div>` : ''}
      </div>
      <div class="text-muted" style="font-size:12px;margin-top:12px">Read-only, ledger-derived view. The full wallet totals include grants and transactions beyond these displayed rows.</div>
    </div>`;
}

function club100LoadWalletDashboard(frm) {
    const field = frm.fields_dict.wallet_dashboard;
    if (!field || !field.$wrapper) return;
    const walletName = frm.doc.name;
    // The dashboard has its own section; occupy its full width for the tables.
    field.$wrapper.closest('.form-column').css({width: '100%', maxWidth: '100%', flex: '0 0 100%'});
    field.$wrapper.html('<p class="text-muted">Loading wallet balances…</p>');
    frappe.call({
        method: 'club100_core.wallet.desk_wallet.wallet_desk_summary',
        type: 'GET',
        args: {wallet_name: walletName},
        callback(response) {
            if (frm.doc.name !== walletName) return;
            if (!response || !response.message) {
                field.$wrapper.html('<p class="text-danger">Wallet balance could not be loaded.</p>');
                return;
            }
            field.$wrapper.html(club100WalletRender(response.message));
        },
        error() {
            if (frm.doc.name === walletName) {
                field.$wrapper.html('<p class="text-danger">Unable to read the wallet summary. Check permissions and server logs.</p>');
            }
        }
    });
}
