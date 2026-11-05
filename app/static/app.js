// Shared diary-form helpers — replaces the inline script block in
// diary_form.html so addTask / addMaterial / addTaskRow / addMaterialRow are
// real functions instead of dead onclick handlers that throw ReferenceError.

function addTask() {
  const container = document.getElementById('tasks-container');
  if (!container) return;
  const idx = container.children.length;
  const div = document.createElement('div');
  div.className = 'form-row';
  div.style.marginBottom = '6px';
  div.innerHTML =
    '<input type="text" name="task_name" placeholder="Công việc" style="flex:3;">' +
    '<input type="number" step="0.01" name="task_qty" placeholder="KL" style="flex:1;">' +
    '<input type="text" name="task_unit" placeholder="ĐVT" style="flex:1;">' +
    '<input type="number" step="0.01" name="task_pct" placeholder="% lũy kế" style="flex:1;">' +
    '<button type="button" class="btn sm" style="color:var(--danger);" ' +
    'onclick="event.preventDefault(); this.parentElement.remove();">✕</button>';
  container.appendChild(div);
}

function addMaterial() {
  const container = document.getElementById('materials-container');
  if (!container) return;
  const div = document.createElement('div');
  div.className = 'form-row';
  div.style.marginBottom = '6px';
  div.innerHTML =
    '<input type="text" name="mat_name" placeholder="Tên VL" style="flex:2;">' +
    '<input type="text" name="mat_unit" placeholder="ĐVT" style="flex:1;">' +
    '<input type="number" step="0.01" name="mat_qty" placeholder="SL" style="flex:1;">' +
    '<button type="button" class="btn sm" style="color:var(--danger);" ' +
    'onclick="event.preventDefault(); this.parentElement.remove();">✕</button>';
  container.appendChild(div);
}

// Render pre-existing rows (edit mode) so they survive a round trip.
function renderExistingTasks(tasks) {
  const container = document.getElementById('tasks-container');
  if (!container || !tasks) return;
  tasks.forEach(function (t) {
    const div = document.createElement('div');
    div.className = 'form-row';
    div.style.marginBottom = '6px';
    div.innerHTML =
      '<input type="text" name="task_name" value="' + escapeHtml(t.task_name || '') + '" placeholder="Công việc" style="flex:3;">' +
      '<input type="number" step="0.01" name="task_qty" value="' + (t.quantity || '') + '" placeholder="KL" style="flex:1;">' +
      '<input type="text" name="task_unit" value="' + escapeHtml(t.unit || '') + '" placeholder="ĐVT" style="flex:1;">' +
      '<input type="number" step="0.01" name="task_pct" value="' + (t.cumulative_pct || '') + '" placeholder="% lũy kế" style="flex:1;">' +
      '<button type="button" class="btn sm" style="color:var(--danger);" ' +
      'onclick="event.preventDefault(); this.parentElement.remove();">✕</button>';
    container.appendChild(div);
  });
}

function renderExistingMaterials(materials) {
  const container = document.getElementById('materials-container');
  if (!container || !materials) return;
  materials.forEach(function (m) {
    const div = document.createElement('div');
    div.className = 'form-row';
    div.style.marginBottom = '6px';
    div.innerHTML =
      '<input type="text" name="mat_name" value="' + escapeHtml(m.name || '') + '" placeholder="Tên VL" style="flex:2;">' +
      '<input type="text" name="mat_unit" value="' + escapeHtml(m.unit || '') + '" placeholder="ĐVT" style="flex:1;">' +
      '<input type="number" step="0.01" name="mat_qty" value="' + (m.quantity || '') + '" placeholder="SL" style="flex:1;">' +
      '<button type="button" class="btn sm" style="color:var(--danger);" ' +
      'onclick="event.preventDefault(); this.parentElement.remove();">✕</button>';
    container.appendChild(div);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}