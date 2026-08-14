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
  return JSON.parse(fs.readFileSync(DB_PATH, 'utf8'));
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

function publicUser(u) {
  return { username: u.username, groupId: u.groupId, role: u.role };
}

app.post('/api/signup', async (req, res) => {
  const { username, password, mode, groupName, groupCode } = req.body || {};
  if (!username || !password) return res.status(400).json({ error: 'נא למלא שם משתמש וסיסמה' });

  const db = readDB();
  const uname = String(username).trim();
  if (db.users[uname]) return res.status(400).json({ error: 'שם המשתמש הזה כבר תפוס' });

  let groupId, role;
  if (mode === 'joinGroup') {
    const code = String(groupCode || '').trim().toUpperCase();
    const group = db.groups[code];
    if (!group) return res.status(400).json({ error: 'קוד קבוצה לא נמצא' });
    groupId = code;
    role = 'member';
    if (!group.members.includes(uname)) group.members.push(uname);
  } else {
    if (!groupName || !groupName.trim()) return res.status(400).json({ error: 'נא להזין שם לקבוצה' });
    groupId = genCode();
    role = 'admin';
    db.groups[groupId] = {
      id: groupId,
      name: groupName.trim(),
      code: groupId,
      adminUsername: uname,
      members: [uname],
    };
    db.tasks[groupId] = [];
    db.notifications[groupId] = [];
  }

  const passwordHash = await bcrypt.hash(password, 10);
  const user = { username: uname, passwordHash, groupId, role };
  db.users[uname] = user;
  writeDB(db);
  res.json({ user: publicUser(user), group: db.groups[groupId] });
});

app.post('/api/login', async (req, res) => {
  const { username, password } = req.body || {};
  if (!username || !password) return res.status(400).json({ error: 'נא למלא שם משתמש וסיסמה' });
  const db = readDB();
  const user = db.users[String(username).trim()];
  if (!user) return res.status(400).json({ error: 'שם משתמש לא קיים' });
  const ok = await bcrypt.compare(password, user.passwordHash);
  if (!ok) return res.status(400).json({ error: 'סיסמה שגויה' });
  res.json({ user: publicUser(user), group: db.groups[user.groupId] });
});

// שימוש רק לכניסה האוטומטית אחרי הפעם הראשונה, על סמך שם משתמש שנשמר במכשיר עצמו
app.get('/api/whoami/:username', (req, res) => {
  const db = readDB();
  const user = db.users[req.params.username];
  if (!user) return res.status(404).json({ error: 'not found' });
  res.json({ user: publicUser(user), group: db.groups[user.groupId] });
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

app.post('/api/tasks/:groupId', (req, res) => {
  const db = readDB();
  const groupId = req.params.groupId;
  const group = db.groups[groupId];
  if (!group) return res.status(404).json({ error: 'group not found' });
  const { title, note, assignee, byUser } = req.body || {};
  if (!title || !title.trim()) return res.status(400).json({ error: 'נא להזין כותרת' });

  const task = {
    id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
    title: title.trim(),
    note: (note || '').trim(),
    assignee,
    done: false,
    createdAt: Date.now(),
  };
  db.tasks[groupId] = db.tasks[groupId] || [];
  db.tasks[groupId].push(task);

  if (assignee && assignee !== byUser) {
    db.notifications[groupId] = db.notifications[groupId] || [];
    db.notifications[groupId].push({
      id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
      toUser: assignee,
      fromUser: byUser,
      type: 'assigned',
      taskTitle: task.title,
      ts: Date.now(),
      read: false,
    });
  }

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

  db.notifications[groupId] = db.notifications[groupId] || [];
  if (assignee !== undefined && assignee !== prev.assignee && assignee !== byUser) {
    db.notifications[groupId].push({
      id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
      toUser: assignee, fromUser: byUser, type: 'assigned', taskTitle: updated.title, ts: Date.now(), read: false,
    });
  }
  if (done === true && prev.done === false && group.adminUsername !== byUser) {
    db.notifications[groupId].push({
      id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
      toUser: group.adminUsername, fromUser: byUser, type: 'done', taskTitle: updated.title, ts: Date.now(), read: false,
    });
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
