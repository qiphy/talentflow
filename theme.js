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
