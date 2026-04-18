function toggleTheme() {
  const htmlElement = document.documentElement;
  const currentTheme = htmlElement.getAttribute('data-theme');
  const themeBtn = document.getElementById('themeToggle');

  if (currentTheme === 'dark') {
    htmlElement.setAttribute('data-theme', 'light');
    themeBtn.textContent = '🌙';
    localStorage.setItem('theme', 'light');
  } else {
    htmlElement.setAttribute('data-theme', 'dark');
    themeBtn.textContent = '☀️';
    localStorage.setItem('theme', 'dark');
  }


  /* ==============================
    PAGE-SPECIFIC UPDATES
    ============================== */

  const gridColor = newTheme === 'dark'
    ? 'rgba(255,255,255,0.05)'
    : 'rgba(0,0,0,0.06)';

  const tickColor = newTheme === 'dark'
    ? '#6e6e73'
    : '#999';

  if (typeof trendChart !== "undefined" && trendChart) {
    trendChart.options.scales.x.grid.color = gridColor;
    trendChart.options.scales.y.grid.color = gridColor;
    trendChart.options.scales.x.ticks.color = tickColor;
    trendChart.options.scales.y.ticks.color = tickColor;
    trendChart.update();
  }
}

// Check for saved user preference on page load
window.addEventListener('load', () => {
  const savedTheme = localStorage.getItem('theme');
  if (savedTheme === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
    const btn = document.getElementById('themeToggle');
    if (btn) btn.textContent = '☀️';
  }
});