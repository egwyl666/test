// «Настройки» → «Пользователи»: список, роли (готовые и свои, права галочками), новый пароль тому, кто забыл
// свой, блокировка и удаление.
// Логины и имена — данные пользователя (translate="no").

let usersData = { items: [], roles: [] };

function roleOptions(selected) {
  return usersData.roles.map((r) => `<option value="${esc(r.id)}"${r.id === selected ? " selected" : ""}>${esc(r.label)}</option>`).join("");
}

function renderUsers() {
  const me = usersData.me || {};
  renderRoles();
  const addRole = $("#user-add").role;
  const keep = addRole.value || "manager";
  addRole.innerHTML = roleOptions(keep);
  $("#users-table").innerHTML = `<thead><tr><th>Логин</th><th>Имя</th><th>Роль</th><th>Последний вход</th><th></th></tr></thead><tbody>` +
    usersData.items.map((u) => `<tr data-id="${u.id}"${u.disabled ? ' class="muted"' : ""}>
      <td><b translate="no">${esc(u.login)}</b>${u.id === me.id ? ` <span class="small muted">(это вы)</span>` : ""}</td>
      <td><input class="user-name" value="${esc(u.name)}" translate="no" style="min-width:110px"></td>
      <td><select class="user-role">${roleOptions(u.role)}</select>${u.disabled ? `<div class="small err-text">заблокирован</div>` : ""}</td>
      <td class="small">${u.last_login_at ? esc(formatDate(u.last_login_at)) : "—"}</td>
      <td><div class="actions">
        <button class="btn small" data-act="password">Пароль</button>
        ${u.id === me.id ? "" : `<button class="btn small" data-act="toggle">${u.disabled ? "Разблокировать" : "Заблокировать"}</button>
        <button class="btn small danger" data-act="delete">Удалить</button>`}
      </div></td></tr>`).join("") + "</tbody>";
}

// Роли: строка — роль, столбцы — права. «Администратор» не меняется (все права).
function permBoxes(perms, disabled) {
  return usersData.perms.map((p) => `<label class="perm-box" title="${esc(p.hint)}"><input type="checkbox" data-perm-id="${esc(p.id)}"${perms.includes(p.id) ? " checked" : ""}${disabled ? " disabled" : ""}> <span>${esc(p.label)}</span></label>`).join("");
}

function renderRoles() {
  $("#roles-table").innerHTML = `<tbody>` + usersData.roles.map((r) => `<tr data-role="${esc(r.id)}">
    <td style="min-width:150px">${r.locked ? `<b>${esc(r.label)}</b>` : `<input class="role-label" value="${esc(r.label)}">`}
      <div class="small muted">${r.users ? `пользователей: ${r.users}` : "никто не пользуется"}${r.builtin ? "" : " · своя"}</div></td>
    <td><div class="perm-list">${r.locked ? `<span class="small">Все права, в том числе настройки и пользователи</span>` : permBoxes(r.perms, false)}</div></td>
    <td>${r.locked ? "" : `<button class="btn small danger" data-role-del>Удалить</button>`}</td></tr>`).join("") + "</tbody>";
  if (!$("#role-add-perms").children.length) $("#role-add-perms").innerHTML = permBoxes([], false);
}

function rolePerms(box) {
  return [...box.querySelectorAll("[data-perm-id]:checked")].map((i) => i.dataset.permId);
}

async function saveRole(id, data, done = "Роль сохранена") {
  try {
    await api(`/api/roles/${id}`, { method: "PATCH", json: data });
    toast(done, "ok");
  } catch (err) { toast(err.message, "error"); }
  await loadUsers();
}

$("#roles-table").addEventListener("change", (e) => {
  const row = e.target.closest("tr[data-role]");
  if (!row) return;
  if (e.target.matches("[data-perm-id]")) saveRole(row.dataset.role, { perms: rolePerms(row) });
  if (e.target.classList.contains("role-label")) saveRole(row.dataset.role, { label: e.target.value });
});

$("#roles-table").addEventListener("click", async (e) => {
  const row = e.target.closest("tr[data-role]");
  if (!row || !e.target.closest("[data-role-del]")) return;
  const role = usersData.roles.find((r) => r.id === row.dataset.role);
  if (!confirm(`Удалить роль «${role.label}»?`)) return;
  try {
    await api(`/api/roles/${role.id}`, { method: "DELETE" });
    toast("Роль удалена", "ok");
  } catch (err) { toast(err.message, "error"); }
  await loadUsers();
});

$("#role-add").addEventListener("submit", () => busy($("#role-add-go"), async () => {
  const f = $("#role-add");
  try {
    await api("/api/roles", { method: "POST", json: { label: f.label.value, perms: rolePerms(f) } });
    toast("Роль добавлена", "ok");
    f.reset();
    await loadUsers();
  } catch (err) { toast(err.message, "error"); }
}));

async function loadUsers() {
  try {
    const [list, me] = await Promise.all([api("/api/users"), api("/api/me")]);
    usersData = { ...list, me };
    renderUsers();
  } catch (err) {
    if (err.status === 403) $("#users").classList.add("hidden");
    else if (!err.network) toast(err.message, "error");
  }
}

async function patchUser(id, data, done = "Сохранено") {
  try {
    await api(`/api/users/${id}`, { method: "PATCH", json: data });
    toast(done, "ok");
  } catch (err) { toast(err.message, "error"); }
  await loadUsers();
}

$("#users-table").addEventListener("change", (e) => {
  const row = e.target.closest("tr[data-id]");
  if (!row) return;
  if (e.target.classList.contains("user-role")) patchUser(row.dataset.id, { role: e.target.value }, "Роль изменена");
  if (e.target.classList.contains("user-name")) patchUser(row.dataset.id, { name: e.target.value });
});

$("#users-table").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]");
  const row = e.target.closest("tr[data-id]");
  if (!b || !row) return;
  const user = usersData.items.find((u) => String(u.id) === row.dataset.id);
  if (b.dataset.act === "password") {
    const password = prompt(`Новый пароль для «${user.login}» (не меньше ${usersData.min_password} символов). Все входы этого пользователя закончатся:`);
    if (password) await patchUser(user.id, { password }, "Пароль изменён");
  } else if (b.dataset.act === "toggle") {
    if (!user.disabled && !confirm(`Заблокировать «${user.login}»? Он сразу выйдет из программы на всех устройствах.`)) return;
    await patchUser(user.id, { disabled: !user.disabled }, user.disabled ? "Пользователь разблокирован" : "Пользователь заблокирован");
  } else if (b.dataset.act === "delete") {
    if (!confirm(`Удалить пользователя «${user.login}»? Его правки в журнале останутся.`)) return;
    try {
      await api(`/api/users/${user.id}`, { method: "DELETE" });
      toast("Пользователь удалён", "ok");
    } catch (err) { toast(err.message, "error"); }
    await loadUsers();
  }
});

$("#user-add").addEventListener("submit", () => busy($("#user-add-go"), async () => {
  const f = $("#user-add");
  try {
    await api("/api/users", { method: "POST", json: { login: f.login.value, name: f.name.value, role: f.role.value, password: f.password.value } });
    toast("Пользователь добавлен", "ok");
    f.reset();
    await loadUsers();
  } catch (err) {
    toast(err.message, "error");
    if (err.field && f[err.field]) f[err.field].focus();
  }
}));

loadUsers();
