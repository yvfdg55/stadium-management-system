(() => {
  const picker = document.querySelector('[data-client-picker]');
  const search = document.getElementById('client_search');
  const clientId = document.getElementById('client_id');
  const suggestions = document.getElementById('client_suggestions');
  const help = document.getElementById('client-search-help');
  const form = document.querySelector('.booking-form');

  if (!picker || !search || !clientId || !suggestions || !form) return;

  const buttons = [...suggestions.querySelectorAll('.client-suggestion')];
  const normalize = (value) => value
    .normalize('NFKD')
    .replace(/[\u064B-\u065F\u0670\u0640]/g, '')
    .replace(/[أإآٱ]/g, 'ا')
    .replace(/ى/g, 'ي')
    .replace(/ة/g, 'ه')
    .toLocaleLowerCase('ar')
    .trim()
    .replace(/\s+/g, ' ');

  const searchable = buttons.map((button) => normalize(
    `${button.querySelector('strong')?.textContent || ''} ${button.querySelector('span')?.textContent || ''}`,
  ));

  function closeSuggestions() {
    suggestions.hidden = true;
    search.setAttribute('aria-expanded', 'false');
  }

  function filterClients() {
    const query = normalize(search.value);
    const terms = query ? query.split(' ') : [];
    let visibleCount = 0;

    buttons.forEach((button, index) => {
      const matches = terms.every((term) => searchable[index].includes(term));
      button.hidden = !matches;
      if (matches) visibleCount += 1;
    });

    suggestions.hidden = visibleCount === 0;
    search.setAttribute('aria-expanded', String(visibleCount > 0));
    if (help) {
      help.textContent = visibleCount === 0 && query
        ? 'لا توجد نتائج مطابقة؛ جرّب اسمًا أو رقم هاتف آخر.'
        : 'ابدأ بالكتابة للبحث، ثم اختر العميل من النتائج.';
    }
    return visibleCount;
  }

  search.addEventListener('input', () => {
    clientId.value = '';
    search.setCustomValidity('اختر عميلًا من نتائج البحث.');
    filterClients();
  });

  search.addEventListener('focus', filterClients);

  buttons.forEach((button) => button.addEventListener('click', () => {
    clientId.value = button.dataset.clientId || '';
    const name = button.querySelector('strong')?.textContent?.trim() || '';
    const phone = button.querySelector('span')?.textContent?.trim() || '';
    search.value = phone ? `${name} — ${phone}` : name;
    search.setCustomValidity('');
    if (help) help.textContent = 'تم اختيار العميل.';
    closeSuggestions();
  }));

  search.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeSuggestions();
      return;
    }
    if (event.key === 'ArrowDown' && !suggestions.hidden) {
      event.preventDefault();
      suggestions.querySelector('.client-suggestion:not([hidden])')?.focus();
    }
  });

  buttons.forEach((button) => button.addEventListener('keydown', (event) => {
    const visibleButtons = buttons.filter((item) => !item.hidden);
    const currentIndex = visibleButtons.indexOf(button);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const direction = event.key === 'ArrowDown' ? 1 : -1;
      const nextIndex = (currentIndex + direction + visibleButtons.length) % visibleButtons.length;
      visibleButtons[nextIndex]?.focus();
    } else if (event.key === 'Escape') {
      search.focus();
      closeSuggestions();
    }
  }));

  document.addEventListener('click', (event) => {
    if (!picker.contains(event.target)) closeSuggestions();
  });

  form.addEventListener('submit', (event) => {
    if (!clientId.value) {
      event.preventDefault();
      search.setCustomValidity('اختر عميلًا من نتائج البحث.');
      search.reportValidity();
    }
  });
})();
