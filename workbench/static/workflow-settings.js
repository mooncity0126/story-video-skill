/* The skill owns the option catalogue and validates the same submitted values. */
async function openWorkflowSettings(continueToProject = false, loaded) {
  try {
    const settings = loaded || await api('/api/workflow-settings');
    const values = settings.values;
    const field = id => settings.fields.find(item => item.id === id);
    function choices(id) {
      const item = field(id);
      return `<fieldset class="setup-group"><legend>${esc(item.label)}</legend><div class="setup-choices">
        ${item.choices.map(option => `<label class="setup-choice">
          <input type="radio" name="${esc(id)}" value="${esc(option.value)}" ${values[id] === option.value ? 'checked' : ''} required>
          <span><strong>${esc(option.label)}</strong><small>${esc(option.impact)}</small></span></label>`).join('')}
      </div></fieldset>`;
    }
    function apiField(item) {
      return `<div class="form-field"><label for="setup-${item.id}">${esc(item.label)}</label>
        <input type="text" id="setup-${item.id}" name="${item.id}" value="${esc(values[item.id])}" spellcheck="false" maxlength="512" required aria-describedby="help-${item.id}">
        <small id="help-${item.id}">${esc(item.impact)}</small></div>`;
    }
    function advanced(item) {
      const selected = item.choices.find(option => option.value === values[item.id]);
      return `<div class="form-field"><label for="setup-${item.id}">${esc(item.label)}</label>
        <select id="setup-${item.id}" name="${item.id}" aria-describedby="help-${item.id}">
        ${item.choices.map(option => `<option value="${esc(option.value)}" ${option.value === selected.value ? 'selected' : ''}>${esc(option.label)}</option>`).join('')}</select>
        <small id="help-${item.id}" aria-live="polite">${esc(selected.impact)}</small></div>`;
    }
    const extra = settings.fields.filter(item => item.choices && !['scheduling', 'prompt_mode'].includes(item.id));
    const title = settings.configured ? '工作流设置' : '首次工作流设置';
    openModal(`<section class="modal setup-modal" role="dialog" aria-modal="true" aria-labelledby="setup-title">
      <header class="modal-head"><div><h2 id="setup-title">${title}</h2><div class="subtitle">保存后，新建影片将沿用这些选择。可随时回来调整。</div></div>
        <button class="icon-button" data-action="close" aria-label="关闭工作流设置">${icon('close')}</button></header>
      <form id="workflow-setup" class="form-content">
        ${choices('scheduling')}
        <details class="setup-details"><summary>视频模型 API <span id="setup-model-summary">${esc(values.model)}</span></summary>
          <div class="form-grid">${settings.fields.filter(item => item.group === 'api').map(apiField).join('')}</div>
          <p class="hint">默认凭据保存在 <code>api-keys.local.json</code>。新增服务使用专属密钥名称时，在同目录的 <code>密钥名称.local.json</code> 中填写同名条目。</p>
        </details>
        ${choices('prompt_mode')}
        <details class="setup-details"><summary>创作、检查与返工</summary><div class="form-grid">${extra.map(advanced).join('')}</div></details>
        <div id="setup-error" class="error-text" role="alert">${esc(settings.error || '')}</div>
      </form>
      <footer class="modal-foot"><span class="hint">各片单独授权积分，提交视频前仍需确认素材。</span>
        <div class="buttons"><button class="button" data-action="close">稍后设置</button>
        <button class="button primary" type="submit" form="workflow-setup">${icon('save')}保存工作流设置</button></div></footer></section>`,
      {type:'setup'});
    const form = $('#workflow-setup');
    form.addEventListener('input', event => {
      const item = field(event.target.name);
      if (item?.id === 'model') $('#setup-model-summary').textContent = event.target.value;
      if (event.target.tagName === 'SELECT') {
        $('#help-' + item.id).textContent = item.choices.find(option => option.value === event.target.value).impact;
      }
    });
    form.addEventListener('invalid', event => {
      const group = event.target.closest('details');
      if (group) group.open = true;
    }, true);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const button = document.querySelector('[form="workflow-setup"]');
      if (button.disabled) return;
      button.disabled = true;
      try {
        await api('/api/workflow-settings', {method:'PUT', body:JSON.stringify(Object.fromEntries(new FormData(form)))});
        if (!form.isConnected) return;
        closeModal();
        const url = new URL(location.href);
        url.searchParams.delete('setup');
        history.replaceState(null, '', url);
        toast('工作流设置已保存，新项目将使用这些选择。');
        if (continueToProject) openInput(true);
      } catch (error) {
        if (form.isConnected) {
          $('#setup-error').textContent = error.message;
          $('#setup-error').scrollIntoView({block:'nearest'});
        }
      } finally {button.disabled = false;}
    });
  } catch (error) {toast(error.message);}
}

async function newProjectWithSettings() {
  try {
    const settings = await api('/api/workflow-settings');
    if (settings.configured) openInput(true);
    else await openWorkflowSettings(true, settings);
  } catch (error) {toast(error.message);}
}
