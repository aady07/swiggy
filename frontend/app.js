const SENDERS = ["Alice", "Bob", "Priya", "Sam"];
const TOKEN_KEY = "admin_token";

const AVATAR_COLORS = ["av-0", "av-1", "av-2", "av-3", "av-4", "av-5"];

const loginScreen = document.getElementById("login-screen");
const appShell = document.getElementById("app-shell");
const loginForm = document.getElementById("login-form");
const loginUser = document.getElementById("login-user");
const loginPass = document.getElementById("login-pass");
const loginError = document.getElementById("login-error");
const adminLogoutBtn = document.getElementById("admin-logout-btn");

const feed = document.getElementById("message-feed");
const membersGrid = document.getElementById("members-grid");
const senderSelect = document.getElementById("sender-select");
const chatForm = document.getElementById("chat-form");
const messageInput = document.getElementById("message-input");
const composeAvatar = document.getElementById("compose-avatar");
const resolvedList = document.getElementById("resolved-list");
const resolvedEmpty = document.getElementById("resolved-empty");
const attentionSection = document.getElementById("attention-section");
const attentionList = document.getElementById("attention-list");
const subtotalEl = document.getElementById("subtotal");
const itemCountEl = document.getElementById("item-count");
const checkoutBtn = document.getElementById("checkout-btn");
const dummyOrderBtn = document.getElementById("dummy-order-btn");
const checkoutNote = document.getElementById("checkout-note");
const authBadge = document.getElementById("auth-badge");
const sidebarAuth = document.getElementById("sidebar-auth");
const loginBtn = document.getElementById("login-btn");
const addressSelect = document.getElementById("address-select");
const refreshAddressesBtn = document.getElementById("refresh-addresses-btn");
const clearChatBtn = document.getElementById("clear-chat-btn");
const clearDraftBtn = document.getElementById("clear-draft-btn");
const showDebugToggle = document.getElementById("show-debug");
const modal = document.getElementById("modal");
const modalText = document.getElementById("modal-text");
const modalCancel = document.getElementById("modal-cancel");
const modalConfirm = document.getElementById("modal-confirm");

const statSubtotal = document.getElementById("stat-subtotal");
const statItems = document.getElementById("stat-items");
const statMembers = document.getElementById("stat-members");
const statAttention = document.getElementById("stat-attention");
const statAttentionCard = document.getElementById("stat-attention-card");
const statOrders = document.getElementById("stat-orders");
const navOrderCount = document.getElementById("nav-order-count");
const familyCount = document.getElementById("family-count");
const ordersList = document.getElementById("orders-list");
const spendByMember = document.getElementById("spend-by-member");
const topItemsList = document.getElementById("top-items-list");
const ordersSummaryHint = document.getElementById("orders-summary-hint");
const oaTotalSpend = document.getElementById("oa-total-spend");
const oaOrderCount = document.getElementById("oa-order-count");
const oaBiggestName = document.getElementById("oa-biggest-name");
const oaBiggestAmount = document.getElementById("oa-biggest-amount");
const oaTopItem = document.getElementById("oa-top-item");
const oaTopItemMeta = document.getElementById("oa-top-item-meta");
const viewDashboard = document.getElementById("view-dashboard");
const viewOrders = document.getElementById("view-orders");
const modalIcon = document.getElementById("modal-icon");
const modalTitle = document.getElementById("modal-title");
const navItems = document.querySelectorAll(".nav-item[data-view]");
const readyCard = document.getElementById("ready-card");
const sidebarSync = document.getElementById("sidebar-sync");

let ws;
let adminToken = sessionStorage.getItem(TOKEN_KEY) || "";
let appBooted = false;
let draftSnapshot = { items: [], estimated_subtotal: 0, resolved_count: 0, unresolved_count: 0 };
let authenticated = false;
let addressId = null;
let dashboardCache = { members: [], messages: [], orders: [] };
let analyticsCache = null;
let highlightOrderId = null;
let expandedOrderId = null;
let checkoutMode = "instamart";

function authHeaders(headers = {}) {
  const out = { ...headers };
  if (adminToken) out.Authorization = `Bearer ${adminToken}`;
  return out;
}

async function apiFetch(url, options = {}) {
  const headers = authHeaders({ ...(options.headers || {}) });
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(url, { ...options, headers });
  if (res.status === 401 && !url.includes("/api/admin/login")) {
    handleAdminLogout();
  }
  return res;
}

function showLoginScreen() {
  loginScreen?.classList.remove("hidden");
  appShell?.classList.add("hidden");
}

function showAppShell() {
  loginScreen?.classList.add("hidden");
  appShell?.classList.remove("hidden");
}

function handleAdminLogout() {
  adminToken = "";
  sessionStorage.removeItem(TOKEN_KEY);
  appBooted = false;
  if (ws) {
    ws.close();
    ws = null;
  }
  showLoginScreen();
}

async function tryAdminSession() {
  if (!adminToken) {
    showLoginScreen();
    return false;
  }
  try {
    const res = await apiFetch("/api/admin/session");
    if (!res.ok) {
      handleAdminLogout();
      return false;
    }
    showAppShell();
    bootApp();
    return true;
  } catch {
    showLoginScreen();
    return false;
  }
}

loginForm?.addEventListener("submit", async (e) => {
  e.preventDefault();
  loginError?.classList.add("hidden");
  const res = await fetch("/api/admin/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: loginUser.value.trim(),
      password: loginPass.value,
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (loginError) {
      loginError.textContent = data.detail || "Invalid username or password";
      loginError.classList.remove("hidden");
    }
    return;
  }
  adminToken = data.token;
  sessionStorage.setItem(TOKEN_KEY, adminToken);
  loginPass.value = "";
  showAppShell();
  bootApp();
});

adminLogoutBtn?.addEventListener("click", handleAdminLogout);

function avatarClass(name) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
}

function initials(name) {
  return (name || "?")
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

function initSenders() {
  SENDERS.forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    senderSelect.appendChild(opt);
  });
  updateComposeAvatar();
}

function updateComposeAvatar() {
  const name = senderSelect.value || "A";
  composeAvatar.textContent = initials(name);
  composeAvatar.className = `compose-avatar ${avatarClass(name)}`;
}

senderSelect?.addEventListener("change", updateComposeAvatar);

function connectWebSocket() {
  if (!adminToken) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(
    `${proto}://${location.host}/ws?token=${encodeURIComponent(adminToken)}`
  );

  ws.onmessage = (event) => {
    const { type, payload } = JSON.parse(event.data);
    if (type === "message") {
      appendMessage(payload);
      refreshDashboard();
    }
    if (type === "draft_update") {
      renderDraft(payload);
      refreshDashboard();
    }
    if (type === "chat_cleared") clearFeed();
    if (type === "auth_status") updateAuth(payload);
    if (type === "checkout_result") handleCheckoutResult(payload);
    if (type === "error") appendSystemMessage(payload.message || "Error");
    if (type === "address_selected") {
      addressId = payload.address_id;
      addressSelect.value = addressId;
    }
  };

  ws.onclose = () => setTimeout(connectWebSocket, 2000);
}

function clearFeed() {
  feed.innerHTML = "";
  showFeedEmpty();
}

function showFeedEmpty() {
  if (feed.children.length) return;
  feed.innerHTML = `
    <div class="feed-empty">
      <div class="feed-empty-icon">💬</div>
      <div>No activity yet</div>
      <div style="font-size:0.75rem;margin-top:0.25rem">Family messages from WhatsApp, Telegram & web appear here</div>
    </div>
  `;
}

function channelBadge(channel) {
  const ch = (channel || "web").toLowerCase();
  const icons = { whatsapp: "WA", telegram: "TG", web: "Web" };
  return `<span class="channel-badge channel-${ch}">${icons[ch] || "Web"}</span>`;
}

function channelIcon(ch) {
  const map = { whatsapp: "📱", telegram: "✈️", web: "🌐" };
  return map[ch] || "🌐";
}

function renderStats(cart, members) {
  const subtotal = cart?.estimated_subtotal || draftSnapshot.estimated_subtotal || 0;
  const items = cart?.resolved_count ?? draftSnapshot.resolved_count ?? 0;
  const unresolved = cart?.unresolved_count ?? draftSnapshot.unresolved_count ?? 0;
  const memberCount = (members || []).length;

  statSubtotal.textContent = `₹${subtotal.toFixed(0)}`;
  statItems.textContent = String(items);
  statMembers.textContent = String(memberCount);
  statAttention.textContent = String(unresolved);
  statAttentionCard.classList.toggle("stat-alert", unresolved > 0);
  if (familyCount) {
    familyCount.textContent = `${memberCount} member${memberCount === 1 ? "" : "s"}`;
  }
}

function switchView(view) {
  navItems.forEach((el) => {
    el.classList.toggle("active", el.dataset.view === view);
  });
  viewDashboard.classList.toggle("hidden", view !== "dashboard");
  viewOrders.classList.toggle("hidden", view !== "orders");
  if (view === "cart") {
    switchView("dashboard");
    document.getElementById("cart-panel")?.scrollIntoView({ behavior: "smooth" });
    return;
  }
  const titles = {
    dashboard: ["Dashboard", "Live view of your family cart across all channels"],
    orders: ["Orders & Analytics", "Past orders, spend breakdown, and most ordered items"],
  };
  const [title, sub] = titles[view] || titles.dashboard;
  document.querySelector(".topbar h1").innerHTML = `<span class="title-icon">${view === "orders" ? "📊" : "📈"}</span> ${title}`;
  document.querySelector(".topbar-sub").textContent = sub;
  if (view === "orders") {
    loadOrdersPage();
  }
}

navItems.forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

function formatOrderDateFull(ts) {
  if (!ts) return "Unknown date";
  try {
    const d = new Date(ts.includes("T") ? ts : ts + "Z");
    return d.toLocaleString([], {
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return ts;
  }
}

function toggleOrderCard(orderId) {
  expandedOrderId = expandedOrderId === orderId ? null : orderId;
  ordersList.querySelectorAll(".order-card").forEach((card) => {
    const id = Number(card.dataset.orderId);
    const open = id === expandedOrderId;
    card.classList.toggle("expanded", open);
    const details = card.querySelector(".order-card-details");
    const chevron = card.querySelector(".order-chevron");
    if (details) details.classList.toggle("hidden", !open);
    if (chevron) chevron.textContent = open ? "▲" : "▼";
  });
}

function renderOrders(orders, { highlightId = null } = {}) {
  ordersList.innerHTML = "";
  const list = orders || [];
  statOrders.textContent = String(list.length);
  navOrderCount.textContent = String(list.length);

  if (!list.length) {
    ordersList.innerHTML = `
      <div class="orders-empty">
        <div style="font-size:2rem;margin-bottom:0.5rem">📋</div>
        No orders yet — place a dummy order or Instamart checkout
      </div>
    `;
    return;
  }

  if (highlightId != null) {
    expandedOrderId = highlightId;
  }

  list.forEach((order) => {
    const card = document.createElement("div");
    const expanded = order.id === expandedOrderId;
    card.className = "order-card" + (expanded ? " expanded" : "");
    if (highlightId && order.id === highlightId) {
      card.classList.add("order-card-new");
    }
    card.dataset.orderId = String(order.id);

    const type = order.order_type || "instamart";
    const typeLabel = type === "dummy" ? "Dummy" : "Instamart";
    const items = order.items || [];
    const previewNames = items
      .slice(0, 2)
      .map((i) => i.resolved_name || i.raw_query || i.name || "Item")
      .join(", ");
    const moreCount = items.length > 2 ? ` +${items.length - 2} more` : "";

    const itemLines = items
      .map((i) => {
        const name = i.resolved_name || i.raw_query || i.name || "Item";
        const qty = i.quantity || i.merged_qty || 1;
        const unit = i.unit_price != null ? Number(i.unit_price) : null;
        const lineTotal = unit != null ? unit * qty : null;
        return `<div class="order-item-line"><strong>${escapeHtml(name)}</strong><span>×${qty}${lineTotal != null ? ` · ₹${lineTotal.toFixed(0)}` : ""}</span></div>`;
      })
      .join("");

    const settlementRows = (order.settlement || [])
      .map(
        (s) => `
      <div class="order-settlement-row">
        <span>${escapeHtml(s.sender)}</span>
        <span>~₹${s.estimated_share} · ${escapeHtml((s.items || []).slice(0, 2).join(", "))}</span>
      </div>`
      )
      .join("");

    card.innerHTML = `
      <button type="button" class="order-card-toggle" aria-expanded="${expanded}">
        <div class="order-card-summary">
          <div class="order-card-main">
            <span class="order-id">Order #${order.id}</span>
            <span class="order-type order-type-${type}">${typeLabel}</span>
          </div>
          <div class="order-card-mid">
            <span class="order-date">${formatOrderDateFull(order.placed_at)}</span>
            ${order.placed_by ? `<span class="order-by"> · ${escapeHtml(order.placed_by)}</span>` : ""}
          </div>
          <div class="order-card-preview">${order.item_count || items.length} items${previewNames ? ` · ${escapeHtml(previewNames)}${moreCount}` : ""}</div>
        </div>
        <div class="order-card-total-wrap">
          <span class="order-total">₹${Number(order.total || 0).toFixed(0)}</span>
          <span class="order-chevron">${expanded ? "▲" : "▼"}</span>
        </div>
      </button>
      <div class="order-card-details${expanded ? "" : " hidden"}">
        <div class="order-details-meta">
          <span>Total <strong>₹${Number(order.total || 0).toFixed(0)}</strong></span>
          <span>${order.item_count || items.length} items</span>
          <span>${formatOrderDateFull(order.placed_at)}</span>
        </div>
        <div class="order-items-detail">${itemLines || '<div class="order-item-line"><span>No item details</span></div>'}</div>
        ${
          settlementRows
            ? `<div class="order-settlement"><div class="order-settlement-title">Settlement split</div>${settlementRows}</div>`
            : ""
        }
      </div>
    `;

    card.querySelector(".order-card-toggle")?.addEventListener("click", () => {
      toggleOrderCard(order.id);
    });

    ordersList.appendChild(card);
  });

  if (highlightId) {
    const card = ordersList.querySelector(`[data-order-id="${highlightId}"]`);
    card?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function renderOrderAnalytics(analytics) {
  analyticsCache = analytics || {};
  const a = analyticsCache;
  if (oaTotalSpend) oaTotalSpend.textContent = `₹${Number(a.total_spend || 0).toFixed(0)}`;
  if (oaOrderCount) oaOrderCount.textContent = String(a.total_orders || 0);

  const biggest = a.biggest_spender;
  if (oaBiggestName) {
    oaBiggestName.textContent = biggest ? biggest.sender : "—";
    oaBiggestName.title = biggest ? `₹${Number(biggest.total_spend).toFixed(0)}` : "";
  }
  if (oaBiggestAmount) {
    oaBiggestAmount.textContent = biggest
      ? `₹${Number(biggest.total_spend).toFixed(0)} · ${biggest.share_pct || 0}% of total`
      : "";
  }

  const top = a.top_item;
  if (oaTopItem) {
    oaTopItem.textContent = top ? top.name : "—";
    oaTopItem.title = top ? top.name : "";
  }
  if (oaTopItemMeta) {
    oaTopItemMeta.textContent = top
      ? `${top.order_count} orders · ${top.total_qty} units`
      : "";
  }

  if (ordersSummaryHint) {
    const dummy = a.dummy_orders || 0;
    const real = a.instamart_orders || 0;
    const avg = Number(a.avg_order_value || 0).toFixed(0);
    ordersSummaryHint.textContent =
      a.total_orders > 0
        ? `${dummy} dummy · ${real} Instamart · avg ₹${avg} · tap to expand`
        : "Tap an order to expand";
  }

  renderSpendByMember(a.spend_by_member || []);
  renderTopItems(a.top_items || []);
}

function renderSpendByMember(rows) {
  if (!spendByMember) return;
  spendByMember.innerHTML = "";
  if (!rows.length) {
    spendByMember.innerHTML = `<div class="analytics-empty">No spend data yet — place an order first</div>`;
    return;
  }
  const max = rows[0]?.total_spend || 1;
  rows.forEach((row, idx) => {
    const pct = Math.max(8, (row.total_spend / max) * 100);
    const el = document.createElement("div");
    el.className = "analytics-row";
    el.innerHTML = `
      <div class="analytics-row-top">
        <span class="analytics-name"><span class="analytics-rank">#${idx + 1}</span>${escapeHtml(row.sender)}</span>
        <span class="analytics-meta">₹${Number(row.total_spend).toFixed(0)} · ${row.share_pct}%</span>
      </div>
      <div class="analytics-bar-wrap">
        <div class="analytics-bar" style="width:${pct}%"></div>
      </div>
    `;
    spendByMember.appendChild(el);
  });
}

function renderTopItems(rows) {
  if (!topItemsList) return;
  topItemsList.innerHTML = "";
  if (!rows.length) {
    topItemsList.innerHTML = `<div class="analytics-empty">No items ordered yet</div>`;
    return;
  }
  const max = rows[0]?.order_count || 1;
  rows.slice(0, 10).forEach((row, idx) => {
    const pct = Math.max(8, (row.order_count / max) * 100);
    const el = document.createElement("div");
    el.className = "analytics-row";
    el.innerHTML = `
      <div class="analytics-row-top">
        <span class="analytics-name"><span class="analytics-rank">#${idx + 1}</span>${escapeHtml(row.name)}</span>
        <span class="analytics-meta">${row.order_count}× · ${row.total_qty} units</span>
      </div>
      <div class="analytics-bar-wrap">
        <div class="analytics-bar bar-green" style="width:${pct}%"></div>
      </div>
    `;
    topItemsList.appendChild(el);
  });
}

async function loadOrdersPage() {
  try {
    const res = await apiFetch("/api/orders");
    if (!res.ok) return;
    const data = await res.json();
    const orders = data.orders || data;
    const analytics = data.analytics || computeClientAnalytics(Array.isArray(orders) ? orders : []);
    renderOrders(Array.isArray(orders) ? orders : [], { highlightId: highlightOrderId });
    renderOrderAnalytics(analytics);
    highlightOrderId = null;
  } catch {
    /* ignore */
  }
}

function computeClientAnalytics(orders) {
  if (!orders.length) return { total_orders: 0, total_spend: 0, spend_by_member: [], top_items: [] };
  const totalSpend = orders.reduce((s, o) => s + Number(o.total || 0), 0);
  return {
    total_orders: orders.length,
    total_spend: totalSpend,
    avg_order_value: totalSpend / orders.length,
    spend_by_member: [],
    top_items: [],
  };
}

function formatOrderDate(ts) {
  if (!ts) return "";
  try {
    return new Date(ts.includes("T") ? ts : ts + "Z").toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return ts;
  }
}

function renderMembers(members) {
  membersGrid.innerHTML = "";
  if (!members || !members.length) {
    membersGrid.innerHTML = `
      <div class="member-empty">
        <span class="member-empty-icon">👨‍👩‍👧</span>
        No family members yet — add items via WhatsApp or Telegram
      </div>
    `;
    return;
  }

  members.forEach((m) => {
    const card = document.createElement("div");
    card.className = "member-card";
    const av = avatarClass(m.name);
    const itemList = m.items || [];
    const channels = (m.channels || [])
      .map((c) => channelBadge(c))
      .join("");
    const thumbs = itemList
      .slice(0, 4)
      .map((item) => `<span class="member-thumb" title="${escapeHtml(item)}">${productEmoji(item)}</span>`)
      .join("");

    card.innerHTML = `
      <div class="member-top">
        <div class="member-avatar ${av}">${initials(m.name)}</div>
        <div>
          <div class="member-name">${escapeHtml(m.name)}</div>
          <div class="member-item-count">${itemList.length} item${itemList.length === 1 ? "" : "s"} in cart</div>
        </div>
      </div>
      <div class="member-channels">${channels || channelBadge("web")}</div>
      ${thumbs ? `<div class="member-thumbs">${thumbs}</div>` : `<div class="member-items">Waiting to add…</div>`}
    `;
    membersGrid.appendChild(card);
  });
}

function appendMessage(msg) {
  const empty = feed.querySelector(".feed-empty");
  if (empty) empty.remove();

  const isDebug = msg.sender === "Debug";
  const isSystem = msg.sender === "System";
  const div = document.createElement("div");
  div.className = "activity-item" + (isDebug ? " debug" : isSystem ? " system" : "");

  const av = isSystem || isDebug ? "av-0" : avatarClass(msg.sender);
  const ch = msg.channel ? channelBadge(msg.channel) : "";
  const preview = (msg.text || "").split(/[,\n]/)[0].slice(0, 80);

  div.innerHTML = `
    <div class="activity-bubble">
      <div class="activity-meta">
        <span class="activity-sender">${escapeHtml(msg.sender)}</span>
        ${ch}
        <span class="activity-time">${formatTime(msg.created_at)}</span>
      </div>
      <div class="activity-text">${escapeHtml(isSystem ? msg.text : preview)}</div>
    </div>
  `;
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
}

function appendSystemMessage(text) {
  appendMessage({ sender: "System", text, created_at: new Date().toISOString() });
}

function productEmoji(name) {
  const n = (name || "").toLowerCase();
  if (/lay|chip|kurkure/.test(n)) return "🥔";
  if (/oreo|biscuit|cookie|hide/.test(n)) return "🍪";
  if (/pickle|achaar/.test(n)) return "🫙";
  if (/milk|doodh|curd/.test(n)) return "🥛";
  if (/atta|flour|wheat/.test(n)) return "🌾";
  if (/rice|chawal|basmati/.test(n)) return "🍚";
  if (/shirt|tshirt|tee/.test(n)) return "👕";
  if (/charger|cable|phone|earphone/.test(n)) return "🔌";
  if (/shampoo|soap/.test(n)) return "🧴";
  return "🛒";
}

function formatTime(ts) {
  if (!ts) return "Just now";
  try {
    const d = new Date(ts.includes("T") ? ts : ts + "Z");
    const now = new Date();
    const diff = (now - d) / 1000;
    if (diff < 60) return "Just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  } catch {
    return ts;
  }
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function updateAuth(status) {
  authenticated = status.authenticated;
  if (authenticated) {
    authBadge.textContent = "Connected";
    authBadge.className = "badge badge-ok";
    sidebarAuth.textContent = "✓ Connected";
    sidebarAuth.className = "status-chip status-ok";
    if (sidebarSync) sidebarSync.textContent = "Last sync: just now";
    loginBtn.textContent = "Re-connect";
    refreshAddressesBtn.disabled = false;
    loadAddresses();
  } else {
    authBadge.textContent = "Offline";
    authBadge.className = "badge badge-warn";
    sidebarAuth.textContent = "Not connected";
    sidebarAuth.className = "status-chip status-warn";
    if (sidebarSync) sidebarSync.textContent = "";
    loginBtn.textContent = "Connect Swiggy";
    addressSelect.disabled = true;
    refreshAddressesBtn.disabled = true;
  }
  updateCheckoutButton();
}

async function loadSettings() {
  const res = await apiFetch("/api/settings");
  const data = await res.json();
  addressId = data.address_id;
  if (data.senders && data.senders.length) {
    senderSelect.innerHTML = "";
    data.senders.forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      senderSelect.appendChild(opt);
    });
    updateComposeAvatar();
  }
}

async function loadAddresses() {
  try {
    const res = await apiFetch("/api/addresses");
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      appendSystemMessage(data.detail || "Could not load Swiggy addresses.");
      return;
    }
    addressSelect.innerHTML = '<option value="">Delivery address…</option>';
    const list = data.addresses || [];
    if (!list.length) {
      addressSelect.disabled = true;
      refreshAddressesBtn.disabled = !authenticated;
      return;
    }
    list.forEach((addr) => {
      const id = addr.id || addr.addressId;
      if (!id) return;
      const label = addr.label || addr.tag || addr.title || "Address";
      const line = addr.address || addr.displayAddress || addr.fullAddress || "";
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = `${label} — ${line}`.trim().slice(0, 80) || label;
      addressSelect.appendChild(opt);
    });
    addressSelect.disabled = false;
    refreshAddressesBtn.disabled = false;
    if (addressId) addressSelect.value = addressId;
  } catch {
    appendSystemMessage("Failed to load addresses.");
  }
}

async function clearSession({ chat = false, draft = false }) {
  const label = chat && draft ? "activity and cart" : chat ? "activity" : "cart";
  if (!confirm(`Clear ${label}? This cannot be undone.`)) return;

  await apiFetch("/api/clear", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat, draft }),
  });

  if (chat) clearFeed();
  if (draft) {
    renderDraft({
      items: [],
      estimated_subtotal: 0,
      resolved_count: 0,
      unresolved_count: 0,
    });
  }
}

refreshAddressesBtn.addEventListener("click", loadAddresses);
clearChatBtn.addEventListener("click", () => clearSession({ chat: true, draft: false }));
clearDraftBtn.addEventListener("click", () => clearSession({ chat: false, draft: true }));

showDebugToggle.addEventListener("change", () => {
  document.body.classList.toggle("show-debug", showDebugToggle.checked);
});

window.addEventListener("focus", () => {
  if (authenticated) loadAddresses();
});

addressSelect.addEventListener("change", async () => {
  const id = addressSelect.value;
  if (!id) return;
  await apiFetch("/api/address", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ address_id: id }),
  });
  addressId = id;
  appendSystemMessage("Delivery address updated.");
  updateCheckoutButton();
});

function renderDraft(snapshot) {
  draftSnapshot = snapshot;
  resolvedList.innerHTML = "";
  attentionList.innerHTML = "";

  const subtotal = snapshot.estimated_subtotal || 0;
  subtotalEl.textContent = `₹${subtotal.toFixed(0)}`;
  itemCountEl.textContent = `${snapshot.resolved_count || 0} item${snapshot.resolved_count === 1 ? "" : "s"}`;

  statSubtotal.textContent = `₹${subtotal.toFixed(0)}`;
  statItems.textContent = String(snapshot.resolved_count || 0);
  statAttention.textContent = String(snapshot.unresolved_count || 0);
  statAttentionCard.classList.toggle("stat-alert", (snapshot.unresolved_count || 0) > 0);

  let attentionCount = 0;

  const numbered = snapshot.numbered_items;
  if (numbered && numbered.length) {
    numbered.forEach((item) => resolvedList.appendChild(renderResolvedItem(item)));
    (snapshot.items || []).forEach((item) => {
      if (item.excluded) return;
      if (["needs_clarification", "needs_poll", "out_of_stock"].includes(item.status)) {
        attentionList.appendChild(renderAttentionItem(item));
        attentionCount += 1;
      }
    });
  } else {
    (snapshot.items || []).forEach((item) => {
      if (item.excluded) return;
      if (item.status === "resolved") {
        resolvedList.appendChild(renderResolvedItem(item));
      } else if (["needs_clarification", "out_of_stock"].includes(item.status)) {
        attentionList.appendChild(renderAttentionItem(item));
        attentionCount += 1;
      }
    });
  }

  const hasResolved = resolvedList.children.length > 0;
  resolvedEmpty.classList.toggle("hidden", hasResolved);
  attentionSection.classList.toggle("hidden", attentionCount === 0);

  updateCheckoutButton();
}

function renderResolvedItem(item) {
  const li = document.createElement("li");
  li.className = "cart-line";
  const name = item.resolved_name || item.raw_query;
  const qty = item.merged_qty || 1;
  const lineTotal = item.unit_price != null ? item.unit_price * qty : null;
  const metaParts = [`×${qty}`];
  if (item.pack_size) metaParts.push(item.pack_size);
  const by = (item.requested_by || [])[0];
  const tagClass = by ? avatarClass(by) : "";
  const tagHtml = by
    ? `<span class="member-tag ${tagClass}">${escapeHtml(by)}</span>`
    : "";

  li.innerHTML = `
    <div class="cart-thumb">${productEmoji(name)}</div>
    <div class="cart-line-body">
      <div class="cart-line-top">
        <span class="cart-name">${item.line_number ? `${item.line_number}. ` : ""}${escapeHtml(name)}</span>
        <span class="cart-price">${lineTotal != null ? `₹${lineTotal.toFixed(0)}` : ""}</span>
      </div>
      <div class="cart-meta">${escapeHtml(metaParts.join(" · "))}</div>
      ${tagHtml}
    </div>
  `;
  return li;
}

function renderAttentionItem(item) {
  const li = document.createElement("li");
  li.className = "attention-item";

  const tag =
    item.status === "out_of_stock"
      ? '<span class="cart-tag">Out of stock</span>'
      : item.status === "needs_poll"
        ? '<span class="cart-tag">Group vote</span>'
        : '<span class="cart-tag">Pick brand</span>';

  li.innerHTML = `
    <div class="cart-row-top">
      <span class="cart-name">${escapeHtml(item.raw_query)}</span>
    </div>
    <div class="cart-meta">${tag}</div>
  `;

  const options =
    item.status === "out_of_stock"
      ? item.alternatives || []
      : item.clarification_options || [];

  options.forEach((opt) => {
    const btn = document.createElement("button");
    btn.className = "option-btn";
    btn.type = "button";
    btn.textContent = `${opt.name}${opt.unit_price != null ? ` · ₹${opt.unit_price}` : ""}`;
    btn.onclick = () => resolveItem(item.id, opt.spin_id);
    li.appendChild(btn);
  });

  const exclude = document.createElement("button");
  exclude.className = "option-btn ghost";
  exclude.type = "button";
  exclude.textContent = "Exclude";
  exclude.onclick = () => excludeItem(item.id);
  li.appendChild(exclude);

  return li;
}

async function resolveItem(draftItemId, spinId) {
  await apiFetch("/api/draft/resolve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ draft_item_id: draftItemId, spin_id: spinId }),
  });
}

async function excludeItem(draftItemId) {
  await apiFetch("/api/draft/exclude", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ draft_item_id: draftItemId, excluded: true }),
  });
}

function updateCheckoutButton() {
  const hasItems = draftSnapshot.resolved_count > 0;
  const canInstamart = authenticated && addressId && hasItems && draftSnapshot.unresolved_count === 0;
  checkoutBtn.disabled = !canInstamart;
  dummyOrderBtn.disabled = !hasItems || draftSnapshot.unresolved_count > 0;

  const ready = hasItems && draftSnapshot.unresolved_count === 0;
  if (readyCard) readyCard.classList.toggle("hidden", !ready);

  if (!hasItems) {
    checkoutNote.textContent = "";
  } else if (draftSnapshot.unresolved_count > 0) {
    checkoutNote.textContent = `${draftSnapshot.unresolved_count} item(s) need attention first`;
  } else if (!authenticated) {
    checkoutNote.textContent = "Connect Swiggy for real Instamart order";
  } else if (!addressId) {
    checkoutNote.textContent = "Select delivery address for Instamart";
  } else {
    checkoutNote.textContent = "Dummy = archive cart · Instamart = real order";
  }
}

function openCheckoutModal(mode) {
  checkoutMode = mode;
  const count = draftSnapshot.resolved_count || 0;
  const total = draftSnapshot.estimated_subtotal || 0;
  if (mode === "dummy") {
    modalIcon.textContent = "📋";
    modalTitle.textContent = "Place dummy order";
    modalText.textContent = `Archive ${count} item(s) (~₹${total.toFixed(0)}) as Order, clear cart, and start fresh? No Swiggy charge.`;
    modalConfirm.textContent = "Place dummy order";
    modalConfirm.className = "btn btn-dummy";
  } else {
    modalIcon.textContent = "🛵";
    modalTitle.textContent = "Place Instamart order";
    modalText.textContent = `Place real Instamart order with ${count} item(s), ~₹${total.toFixed(0)}?`;
    modalConfirm.textContent = "Place order";
    modalConfirm.className = "btn btn-checkout";
  }
  modal.classList.remove("hidden");
}

checkoutBtn.addEventListener("click", () => openCheckoutModal("instamart"));
dummyOrderBtn.addEventListener("click", () => openCheckoutModal("dummy"));

chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = messageInput.value.trim();
  if (!text) return;
  const sender = senderSelect.value;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "send_message", payload: { sender, text } }));
  } else {
    apiFetch("/api/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sender, text }),
    });
  }
  messageInput.value = "";
});

modalCancel.addEventListener("click", () => modal.classList.add("hidden"));
modal.querySelector(".modal-backdrop")?.addEventListener("click", () => modal.classList.add("hidden"));

modalConfirm.addEventListener("click", async () => {
  modal.classList.add("hidden");
  checkoutBtn.disabled = true;
  dummyOrderBtn.disabled = true;
  const url = checkoutMode === "dummy" ? "/api/checkout/dummy" : "/api/checkout";
  const body =
    checkoutMode === "dummy"
      ? { confirmed: true, placed_by: senderSelect.value || "Admin" }
      : { confirmed: true };
  const res = await apiFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  handleCheckoutResult(data);
  refreshDashboard();
});

function handleCheckoutResult(payload) {
  if (payload.success) {
    const label =
      payload.order_type === "dummy"
        ? `Dummy order #${payload.order_id} placed`
        : payload.swiggy_message || `Order #${payload.order_id} placed.`;
    appendSystemMessage(label);
    if (payload.settlement) {
      payload.settlement.forEach((line) => {
        appendSystemMessage(`${line.sender}: ${line.items.join(", ")} (~₹${line.estimated_share})`);
      });
    }
    highlightOrderId = payload.order_id;
    switchView("orders");
  } else {
    appendSystemMessage(payload.error || "Order failed.");
  }
  updateCheckoutButton();
}

async function refreshDashboard() {
  try {
    const res = await apiFetch("/api/household/dashboard");
    if (!res.ok) return;
    const data = await res.json();
    dashboardCache = data;
    renderMembers(data.members || []);
    statOrders.textContent = String((data.orders || []).length);
    navOrderCount.textContent = String((data.orders || []).length);
    if (!viewOrders.classList.contains("hidden")) {
      await loadOrdersPage();
    }
    renderStats(data.cart, data.members);
  } catch {
    /* ignore */
  }
}

async function loadInitialState() {
  const [dashboardRes, draftRes] = await Promise.all([
    apiFetch("/api/household/dashboard"),
    apiFetch("/api/draft"),
  ]);
  const dashboard = await dashboardRes.json();
  const draft = await draftRes.json();
  dashboardCache = dashboard;

  clearFeed();
  const msgs = (dashboard.messages || []).filter(
    (m) => m.sender !== "Debug" || document.body.classList.contains("show-debug")
  );
  if (msgs.length) {
    msgs.forEach(appendMessage);
  } else {
    showFeedEmpty();
  }

  renderMembers(dashboard.members || []);
  statOrders.textContent = String((dashboard.orders || []).length);
  navOrderCount.textContent = String((dashboard.orders || []).length);
  renderStats(dashboard.cart, dashboard.members);
  renderDraft(draft);
  loadOrdersPage();
}

function bootApp() {
  if (appBooted) return;
  appBooted = true;
  initSenders();
  loadSettings();
  connectWebSocket();
  loadInitialState();
  apiFetch("/api/auth/status")
    .then((r) => r.json())
    .then(updateAuth);
}

tryAdminSession();
