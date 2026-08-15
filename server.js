const express = require('express');
const bcrypt = require('bcryptjs');
const fs = require('fs');
const path = require('path');

const DB_PATH = path.join(__dirname, 'db.json');
const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

function readDB() {
  if (!fs.existsSync(DB_PATH)) {
    return { users: {}, groups: {}, tasks: {}, notifications: {} };
  }
  const db = JSON.parse(fs.readFileSync(DB_PATH, 'utf8'));
  let migrated = false;
  Object.values(db.groups || {}).forEach(g => {
    if (!Array.isArray(g.admins)) {
      g.admins = g.adminUsername ? [g.adminUsername] : [];
      migrated = true;
    }
  });
  if (migrated) writeDB(db);
  return db;
}

function writeDB(db) {
  fs.writeFileSync(DB_PATH, JSON.stringify(db, null, 2));
}

function genCode() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  let s = '';
  for (let i = 0; i < 6; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return s;
}

function genId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

function isAdmin(group, username) {
  return !!(group && group.admins && group.admins.includes(username));
}

function publicUser(u, group) {
  return {
    username: u.username,
    groupId: u.groupId,
    role: isAdmin(group, u.username) ? 'admin' : 'member',
  };
}

function pushNotif(db, groupId, toUser, fromUser, type, taskTitle) {
  if (!toUser || toUser === fromUser) return;
  db.notifications[groupId] = db.notifications[groupId] || [];
  db.notifications[groupId].push({
    id: genId(), toUser, fromUser, type, taskTitle, ts: Date.now(), read: false,
  });
}

app.post('/api/signup', async (req, res) => {
  const { username, password, mode, groupName, groupCode } = req.body || {};
  if (!username || !password) return res.status(400).json({ error: 'נא למלא שם משתמש וסיסמה' });

  const db = readDB();
  const uname = String(username).trim();
  if (db.users[uname]) return res.status(400).json({ error: 'שם המשתמש הזה כבר תפוס' });

  let groupId;
  if (mode === 'joinGroup') {
    const code = String(groupCode || '').trim().toUpperCase();
    const group = db.groups[code];
    if (!group) return res.status(400).json({ error: 'קוד קבוצה לא נמצא' });
    groupId = code;
    if (!group.members.includes(uname)) group.members.push(uname);
  } else {
    if (!groupName || !groupName.trim()) return res.status(400).json({ error: 'נא להזין שם לקבוצה' });
    groupId = genCode();
    db.groups[groupId] = {
      id: groupId,
      name: groupName.trim(),
      code: groupId,
      adminUsername: uname,
      admins: [uname],
      members: [uname],
    };
    db.tasks[groupId] = [];
    db.notifications[groupId] = [];
  }

  const passwordHash = await bcrypt.hash(password, 10);
  const user = { username: uname, passwordHash, groupId };
  db.users[uname] = user;
  writeDB(db);
  res.json({ user: publicUser(user, db.groups[groupId]), group: db.groups[groupId] });
});

app.post('/api/login', async (req, res) => {
  const { username, password } = req.body || {};
  if (!username || !password) return res.status(400).json({ error: 'נא למלא שם משתמש וסיסמה' });
  const db = readDB();
  const user = db.users[String(username).trim()];
  if (!user) return res.status(400).json({ error: 'שם משתמש לא קיים' });
  const ok = await bcrypt.compare(password, user.passwordHash);
  if (!ok) return res.status(400).json({ error: 'סיסמה שגויה' });
  const group = db.groups[user.groupId];
  res.json({ user: publicUser(user, group), group });
});

app.get('/api/whoami/:username', (req, res) => {
  const db = readDB();
  const user = db.users[req.params.username];
  if (!user) return res.status(404).json({ error: 'not found' });
  const group = db.groups[user.groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  res.json({ user: publicUser(user, group), group });
});

app.get('/api/state/:groupId', (req, res) => {
  const db = readDB();
  const group = db.groups[req.params.groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  res.json({
    group,
    tasks: db.tasks[req.params.groupId] || [],
    notifications: db.notifications[req.params.groupId] || [],
  });
});

app.post('/api/user/change-password', async (req, res) => {
  const { username, oldPassword, newPassword } = req.body || {};
  const db = readDB();
  const user = db.users[username];
  if (!user) return res.status(404).json({ error: 'משתמש לא נמצא' });
  const ok = await bcrypt.compare(oldPassword || '', user.passwordHash);
  if (!ok) return res.status(400).json({ error: 'הסיסמה הנוכחית שגויה' });
  if (!newPassword || newPassword.length < 1) return res.status(400).json({ error: 'נא להזין סיסמה חדשה' });
  user.passwordHash = await bcrypt.hash(newPassword, 10);
  writeDB(db);
  res.json({ ok: true });
});

app.post('/api/group/:groupId/promote', (req, res) => {
  const db = readDB();
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  const { username, byUser } = req.body || {};
  if (!isAdmin(group, byUser)) return res.status(403).json({ error: 'רק מנהל יכול לעשות זאת' });
  if (!group.members.includes(username)) return res.status(400).json({ error: 'המשתמש אינו חבר בקבוצה' });
  if (!group.admins.includes(username)) group.admins.push(username);
  writeDB(db);
  res.json({ group });
});

app.post('/api/group/:groupId/remove-member', (req, res) => {
  const db = readDB();
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  const { username, byUser } = req.body || {};
  if (!isAdmin(group, byUser)) return res.status(403).json({ error: 'רק מנהל יכול לעשות זאת' });
  if (username === byUser) return res.status(400).json({ error: 'להסרה עצמית השתמשו ב"עזיבת קבוצה"' });
  group.members = group.members.filter(m => m !== username);
  group.admins = group.admins.filter(a => a !== username);
  delete db.users[username];
  writeDB(db);
  res.json({ group });
});

app.post('/api/group/:groupId/leave', (req, res) => {
  const db = readDB();
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  const { username } = req.body || {};
  const wasOnlyAdmin = isAdmin(group, username) && group.admins.length === 1;
  if (wasOnlyAdmin && group.members.length > 1) {
    return res.status(400).json({ error: 'את/ה המנהל/ת היחיד/ה - קודם מנו מנהל נוסף או מחקו את הקבוצה' });
  }
  group.members = group.members.filter(m => m !== username);
  group.admins = group.admins.filter(a => a !== username);
  delete db.users[username];

  if (group.members.length === 0) {
    delete db.groups[groupId];
    delete db.tasks[groupId];
    delete db.notifications[groupId];
  }
  writeDB(db);
  res.json({ ok: true });
});

app.delete('/api/group/:groupId', (req, res) => {
  const db = readDB();
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  const { byUser } = req.body || {};
  if (!isAdmin(group, byUser)) return res.status(403).json({ error: 'רק מנהל יכול למחוק את הקבוצה' });

  (group.members || []).forEach(m => delete db.users[m]);
  delete db.groups[groupId];
  delete db.tasks[groupId];
  delete db.notifications[groupId];
  writeDB(db);
  res.json({ ok: true });
});

app.post('/api/tasks/:groupId', (req, res) => {
  const db = readDB();
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  const { title, note, assignee, byUser } = req.body || {};
  if (!title || !title.trim()) return res.status(400).json({ error: 'נא להזין כותרת' });

  const task = {
    id: genId(),
    title: title.trim(),
    note: (note || '').trim(),
    assignee,
    done: false,
    createdAt: Date.now(),
  };
  db.tasks[groupId] = db.tasks[groupId] || [];
  db.tasks[groupId].push(task);
  pushNotif(db, groupId, assignee, byUser, 'assigned', task.title);

  writeDB(db);
  res.json({ task });
});

app.put('/api/tasks/:groupId/:taskId', (req, res) => {
  const db = readDB();
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  const list = db.tasks[groupId] || [];
  const idx = list.findIndex(t => t.id === req.params.taskId);
  if (idx === -1) return res.status(404).json({ error: 'task not found' });

  const prev = list[idx];
  const { title, note, assignee, done, byUser } = req.body || {};
  const updated = { ...prev };
  if (title !== undefined) updated.title = title.trim();
  if (note !== undefined) updated.note = note.trim();
  if (assignee !== undefined) updated.assignee = assignee;
  if (done !== undefined) updated.done = done;
  list[idx] = updated;
  db.tasks[groupId] = list;

  if (assignee !== undefined && assignee !== prev.assignee) {
    pushNotif(db, groupId, assignee, byUser, 'assigned', updated.title);
  }
  if (done === true && prev.done === false) {
    (group.admins || []).forEach(adminUser => pushNotif(db, groupId, adminUser, byUser, 'done', updated.title));
  }

  writeDB(db);
  res.json({ task: updated });
});

app.delete('/api/tasks/:groupId/:taskId', (req, res) => {
  const db = readDB();
  const groupId = req.params.groupId;
  db.tasks[groupId] = (db.tasks[groupId] || []).filter(t => t.id !== req.params.taskId);
  writeDB(db);
  res.json({ ok: true });
});

app.post('/api/notifications/:groupId/read', (req, res) => {
  const db = readDB();
  const groupId = req.params.groupId;
  const { username } = req.body || {};
  db.notifications[groupId] = (db.notifications[groupId] || []).map(n =>
    n.toUser === username ? { ...n, read: true } : n
  );
  writeDB(db);
  res.json({ ok: true });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log('Server running on port ' + PORT));
