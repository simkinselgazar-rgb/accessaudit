/* ===================================================================
   AccessAudit - Application JavaScript
   =================================================================== */

/* ------------------------------------------------------------------
   Collapsible sections
   ------------------------------------------------------------------ */
function toggleCollapsible(btn) {
  var expanded = btn.getAttribute('aria-expanded') === 'true';
  btn.setAttribute('aria-expanded', String(!expanded));
  var targetId = btn.getAttribute('aria-controls');
  var target = document.getElementById(targetId);
  if (target) {
    target.classList.toggle('open', !expanded);
  }
}

/* ------------------------------------------------------------------
   Review type toggle (show/hide max pages)
   ------------------------------------------------------------------ */
(function() {
  var radios = document.querySelectorAll('input[name="review_type"]');
  var maxGroup = document.getElementById('max-pages-group');
  var multiGroup = document.getElementById('multi-pages-group');
  var urlGroup = document.getElementById('url-input-group');
  var fileGroup = document.getElementById('file-input-group');
  var orDivider = document.getElementById('url-or-divider');

  function updateReviewType() {
    var selected = document.querySelector('input[name="review_type"]:checked');
    var val = selected ? selected.value : 'single';

    // Show/hide based on review type
    if (maxGroup) {
      maxGroup.style.display = val === 'site' ? '' : 'none';
      if (val === 'site') maxGroup.classList.add('visible');
      else maxGroup.classList.remove('visible');
    }
    if (multiGroup) {
      multiGroup.style.display = val === 'multi' ? '' : 'none';
    }

    // Page rationale: visible for multi and site reviews
    var rationaleGroup = document.getElementById('page-rationale-group');
    if (rationaleGroup) {
      rationaleGroup.style.display = (val === 'multi' || val === 'site') ? '' : 'none';
    }

    // Single page: show URL + file upload
    // Multi-page: show ONLY the multi-URL textarea (hide single URL + file)
    // Site crawl: show ONLY the single URL (hide file upload)
    if (urlGroup) urlGroup.style.display = val === 'multi' ? 'none' : '';
    if (fileGroup) fileGroup.style.display = val === 'single' ? '' : 'none';
    if (orDivider) orDivider.style.display = val === 'single' ? '' : 'none';
  }

  radios.forEach(function(r) {
    r.addEventListener('change', updateReviewType);
  });
  updateReviewType();
})();

/* ------------------------------------------------------------------
   Build FormData helper
   ------------------------------------------------------------------ */
function buildFormData(form, fileOverride) {
  var fd = new FormData();
  var data = new FormData(form);

  // Copy non-file fields
  for (var pair of data.entries()) {
    var el = form.elements[pair[0]];
    if (el && el.type === 'file') continue;
    fd.append(pair[0], pair[1]);
  }

  // If submitting a file, clear URL
  if (fileOverride) {
    fd.delete('url');
    fd.append('pdf_file', fileOverride);
  }

  // Append company logo if present
  var logoInput = form.elements['company_logo'];
  if (logoInput && logoInput.files && logoInput.files.length > 0) {
    fd.append('company_logo', logoInput.files[0]);
  }

  return fd;
}

/* ------------------------------------------------------------------
   Form submission handler
   ------------------------------------------------------------------ */
(function() {
  var form = document.getElementById('review-form');
  if (!form) return;

  form.addEventListener('submit', function(e) {
    e.preventDefault();

    var urlInput = form.elements['url'];
    var fileInput = form.elements['pdf_file'];
    var url = urlInput ? urlInput.value.trim() : '';
    var files = fileInput ? fileInput.files : null;
    var reviewType = document.querySelector('input[name="review_type"]:checked');
    var isSiteCrawl = reviewType && reviewType.value === 'site';

    // Validate: URL or file required
    if (!url && (!files || files.length === 0)) {
      alert('Please enter a URL or upload a document.');
      if (urlInput) urlInput.focus();
      return;
    }

    // URL format validation
    if (url) {
      try {
        var parsed = new URL(url);
        if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
          alert('Please enter a valid HTTP or HTTPS URL.');
          if (urlInput) urlInput.focus();
          return;
        }
      } catch (e) {
        alert('Please enter a valid URL (e.g., https://example.com).');
        if (urlInput) urlInput.focus();
        return;
      }
    }

    // File type validation
    if (files && files.length > 0) {
      var allowedTypes = ['.pdf', '.docx', '.xlsx', '.pptx'];
      for (var fi = 0; fi < files.length; fi++) {
        var fileName = files[fi].name.toLowerCase();
        var valid = allowedTypes.some(function(ext) { return fileName.endsWith(ext); });
        if (!valid) {
          alert('Unsupported file type: ' + files[fi].name + '. Allowed: PDF, DOCX, XLSX, PPTX.');
          return;
        }
      }
    }

    var isMultiPage = reviewType && reviewType.value === 'multi';

    // Site crawl requires URL
    if (isSiteCrawl && !url) {
      alert('Full Site Crawl requires a URL. File uploads are not supported for site crawls.');
      if (urlInput) urlInput.focus();
      return;
    }

    // Multi-page requires URLs in the textarea
    if (isMultiPage) {
      var multiUrlsEl = document.getElementById('multi-urls');
      var multiUrls = (multiUrlsEl ? multiUrlsEl.value : '').trim();
      if (!multiUrls) {
        alert('Multi-Page mode requires at least 2 URLs. Enter one URL per line.');
        if (multiUrlsEl) multiUrlsEl.focus();
        return;
      }
      var urlLines = multiUrls.split('\n').map(function(l) { return l.trim(); }).filter(function(l) { return l.length > 0; });
      if (urlLines.length < 2) {
        alert('Multi-Page mode requires at least 2 URLs.');
        if (multiUrlsEl) multiUrlsEl.focus();
        return;
      }
    }

    var endpoint = isSiteCrawl ? '/review/start-site' : isMultiPage ? '/review/start-multi' : '/review/start';
    var submitBtn = document.getElementById('submit-btn');
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = 'Starting review...';
    }

    // Multi-file support: if multiple files, submit each individually
    if (!isSiteCrawl && files && files.length > 1) {
      var promises = [];
      var fileNames = [];
      for (var i = 0; i < files.length; i++) {
        fileNames.push(files[i].name);
        var fd = buildFormData(form, files[i]);
        (function(name) {
          promises.push(
            fetch(endpoint, { method: 'POST', body: fd })
              .then(function(r) {
                if (!r.ok) {
                  return r.json().catch(function() { return {}; }).then(function(d) {
                    throw new Error(name + ': ' + (d.error || d.detail || ('server returned ' + r.status)));
                  });
                }
                return r.json();
              })
              .then(function(data) { return {ok: true, name: name}; })
              .catch(function(err) { return {ok: false, name: name, error: err.message}; })
          );
        })(files[i].name);
      }
      Promise.all(promises)
        .then(function(results) {
          var failures = results.filter(function(r) { return !r.ok; });
          if (failures.length > 0) {
            var msgs = failures.map(function(f) { return f.name + ': ' + f.error; });
            alert('Some uploads failed:\n' + msgs.join('\n'));
          }
          window.location.href = '/';
        })
        .catch(function(err) {
          alert('Error starting reviews: ' + err.message);
          if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Start Accessibility Review';
          }
        });
      return;
    }

    // Single file or URL submission
    var fd;
    if (files && files.length === 1 && !isSiteCrawl) {
      fd = buildFormData(form, files[0]);
    } else {
      fd = buildFormData(form, null);
    }

    fetch(endpoint, { method: 'POST', body: fd })
      .then(function(r) {
        if (!r.ok) {
          // Backend returns {error: "..."} for validation / security rejections
          // (invalid URL, private IP, upload too large, bad review id, etc).
          // Surface that message instead of a bare status code.
          return r.json().catch(function() { return {}; }).then(function(d) {
            throw new Error(d.error || d.detail || ('Server returned ' + r.status));
          });
        }
        return r.json();
      })
      .then(function(data) {
        if (data.redirect) {
          window.location.href = data.redirect;
        } else if (data.review_id) {
          window.location.href = '/review/' + data.review_id + '/progress';
        } else {
          window.location.href = '/';
        }
      })
      .catch(function(err) {
        alert('Error starting review: ' + err.message);
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Start Accessibility Review';
        }
      });
  });
})();

/* ------------------------------------------------------------------
   Review deletion
   ------------------------------------------------------------------ */
function deleteReview(reviewId) {
  if (!confirm('Delete this review? This cannot be undone.')) return;

  fetch('/api/review/' + reviewId, { method: 'DELETE' })
    .then(function(r) {
      if (!r.ok) return r.json().then(function(d) { throw new Error(d.detail || 'Delete failed'); });
      return r.json();
    })
    .then(function() {
      var card = document.getElementById('review-' + reviewId);
      if (card) {
        card.style.transition = 'opacity .3s';
        card.style.opacity = '0';
        setTimeout(function() { card.remove(); }, 300);
      }
    })
    .catch(function(err) {
      alert('Could not delete review: ' + err.message);
    });
}

function deleteAllReviews() {
  if (!confirm('Delete ALL reviews? This cannot be undone.')) return;

  fetch('/api/reviews', { method: 'DELETE' })
    .then(function(r) { return r.json(); })
    .then(function() {
      window.location.reload();
    })
    .catch(function(err) {
      alert('Could not delete reviews: ' + err.message);
    });
}

/* ------------------------------------------------------------------
   Status polling for home page
   ------------------------------------------------------------------ */
(function() {
  var cards = document.getElementById('review-cards');
  if (!cards) return;

  var activeStatuses = ['queued', 'crawling', 'selecting', 'authenticating', 'capturing', 'testing', 'testing_documents', 'aggregating', 'generating_report'];

  function hasActiveReviews() {
    var items = cards.querySelectorAll('.review-card');
    for (var i = 0; i < items.length; i++) {
      var status = items[i].dataset.status;
      if (activeStatuses.indexOf(status) !== -1) return true;
    }
    return false;
  }

  function pollReviews() {
    if (!hasActiveReviews()) return;

    fetch('/api/reviews')
      .then(function(r) { return r.json(); })
      .then(function(reviews) {
        // If count changed, full reload
        var currentCount = cards.querySelectorAll('.review-card').length;
        if (reviews.length !== currentCount) {
          window.location.reload();
          return;
        }

        reviews.forEach(function(review) {
          var card = document.getElementById('review-' + review.review_id);
          if (!card) return;

          var oldStatus = card.dataset.status;
          if (oldStatus === review.status) return;

          card.dataset.status = review.status;

          // Update badge
          var badge = card.querySelector('.badge');
          if (badge) {
            badge.className = 'badge badge-' + review.status;
            badge.textContent = review.status;
          }

          // Update actions
          var actions = card.querySelector('.actions');
          if (actions) {
            // Remove old link (keep delete button)
            var oldLinks = actions.querySelectorAll('a, span.text-muted');
            oldLinks.forEach(function(el) { el.remove(); });

            var deleteBtn = actions.querySelector('.btn-delete');

            if (review.status === 'complete') {
              var a = document.createElement('a');
              a.href = '/review/' + review.review_id + '/report';
              a.textContent = 'View Report';
              actions.insertBefore(a, deleteBtn);
            } else if (review.status === 'error') {
              var a = document.createElement('a');
              a.href = '/review/' + review.review_id + '/progress';
              a.textContent = 'View Details';
              actions.insertBefore(a, deleteBtn);
            } else if (review.status === 'queued') {
              var s = document.createElement('span');
              s.className = 'text-muted';
              s.style.fontSize = '.8rem';
              s.style.padding = '.3rem 0';
              s.textContent = 'Queued';
              actions.insertBefore(s, deleteBtn);
            } else {
              var a = document.createElement('a');
              a.href = '/review/' + review.review_id + '/progress';
              a.textContent = 'View Progress';
              actions.insertBefore(a, deleteBtn);
            }
          }
        });

        // Schedule next poll if still active
        if (hasActiveReviews()) {
          setTimeout(pollReviews, 5000);
        }
      })
      .catch(function() {
        // Retry on error
        setTimeout(pollReviews, 5000);
      });
  }

  // Start polling
  if (hasActiveReviews()) {
    setTimeout(pollReviews, 5000);
  }
})();

/* ------------------------------------------------------------------
   WebSocket connection for progress page
   ------------------------------------------------------------------ */
var _ws = null;
var _wsPingInterval = null;

function initProgressWebSocket(reviewId) {
  var wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  var wsUrl = wsProtocol + '//' + window.location.host + '/ws/' + reviewId;

  _ws = new WebSocket(wsUrl);

  _ws.onopen = function() {
    // Start ping every 15 seconds
    _wsPingInterval = setInterval(function() {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        _ws.send(JSON.stringify({ type: 'ping' }));
      }
    }, 15000);
  };

  _ws.onmessage = function(event) {
    var msg;
    try {
      msg = JSON.parse(event.data);
    } catch (e) {
      return;
    }
    handleProgressMessage(msg);
  };

  _ws.onerror = function() {
    // Silently handle errors - messages will show stale state
  };

  _ws.onclose = function() {
    if (_wsPingInterval) {
      clearInterval(_wsPingInterval);
      _wsPingInterval = null;
    }
    // Attempt reconnect after 3 seconds, unless complete or error
    var section = document.querySelector('.progress-section');
    var status = section ? section.dataset.initialStatus : '';
    if (status !== 'complete' && status !== 'error') {
      setTimeout(function() {
        initProgressWebSocket(reviewId);
      }, 3000);
    }
  };
}

/* ------------------------------------------------------------------
   Progress message handlers
   ------------------------------------------------------------------ */
var _phaseMessages = {
  queued: 'Waiting for other reviews to finish...',
  crawling: 'Crawling site to discover pages...',
  selecting: 'AI analyzing site and selecting pages for testing...',
  selected: 'Pages selected — preparing to test...',
  authenticating: 'Login detected — please log in to the browser window that opened...',
  capturing: 'Capturing page content (screenshots, DOM, accessibility tree)...',
  testing: 'Running accessibility tests...',
  testing_documents: 'Testing linked documents (PDFs, DOCX, etc.)...',
  aggregating: 'Aggregating results across pages...',
  reviewing: 'Final reviewer (Pro): checking ACR for consistency, calibration, citations, tone...',
  generating_report: 'Generating ACR report...',
  complete: 'Review complete.',
  error: 'An error occurred.',
  cancelled: 'Review was cancelled.'
};

function handleProgressMessage(msg) {
  switch (msg.type) {
    case 'pong':
      // Keep-alive response, no action needed
      break;

    case 'phase':
      updatePhase(msg.phase, msg.total_pages, msg.message);
      break;

    case 'crawl_progress':
      handleCrawlProgress(msg);
      break;

    case 'site_page_start':
      handleSitePageStart(msg);
      break;

    case 'site_page_complete':
      handleSitePageComplete(msg);
      break;

    case 'site_page_error':
      handleSitePageError(msg);
      break;

    case 'test_start':
      handleTestStart(msg);
      break;

    case 'test_complete':
      handleTestComplete(msg);
      break;

    case 'complete':
      handleComplete(msg);
      break;

    case 'error':
      handleError(msg);
      break;

    case 'audit_complete':
      handleAuditComplete(msg);
      break;

    case 'reviewer_complete':
      handleReviewerComplete(msg);
      break;

    case 'cancelled':
    case 'cancelling':
      var badge = document.getElementById('phase-badge');
      if (badge) { badge.textContent = 'cancelled'; badge.className = 'badge badge-cancelled'; }
      var pmsg = document.getElementById('phase-message');
      if (pmsg) pmsg.textContent = msg.message || 'Review cancelled.';
      var cbtn = document.getElementById('cancel-btn');
      if (cbtn) cbtn.style.display = 'none';
      break;
  }
}


function handleAuditComplete(msg) {
  // The backend runs audit_run.audit_review after every review and
  // sends the result here. Bugs = silent data-quality problems that
  // would otherwise ship into the ACR unnoticed (judge-dropped
  // findings, missing element text, parser text-fallback, truncation
  // markers in judge context). Warnings are softer signals.
  var section = document.getElementById('audit-section');
  if (!section) return;

  var bugs = Array.isArray(msg.bugs) ? msg.bugs : [];
  var warns = Array.isArray(msg.warns) ? msg.warns : [];
  var stats = msg.stats || {};

  if (bugs.length === 0 && warns.length === 0) {
    section.innerHTML =
      '<div class="audit-banner audit-ok">' +
      '<strong>Quality audit:</strong> clean — ' +
      (stats.completed || 0) + ' criteria completed, ' +
      (stats.total_findings || 0) + ' findings.' +
      '</div>';
    section.style.display = 'block';
    return;
  }

  var html = '<div class="audit-banner ' +
    (bugs.length > 0 ? 'audit-bugs' : 'audit-warns') + '">';
  html += '<strong>Quality audit:</strong> ';
  if (bugs.length > 0) {
    html += bugs.length + ' bug' + (bugs.length === 1 ? '' : 's');
    if (warns.length > 0) html += ', ';
  }
  if (warns.length > 0) {
    html += warns.length + ' warning' + (warns.length === 1 ? '' : 's');
  }
  html += ' detected in ACR output quality.';

  if (bugs.length > 0) {
    html += '<details open><summary>Bugs (' + bugs.length + ')</summary><ul>';
    for (var i = 0; i < bugs.length; i++) {
      html += '<li>' + escapeHtml(bugs[i]) + '</li>';
    }
    html += '</ul></details>';
  }
  if (warns.length > 0) {
    html += '<details><summary>Warnings (' + warns.length + ')</summary><ul>';
    for (var j = 0; j < warns.length; j++) {
      html += '<li>' + escapeHtml(warns[j]) + '</li>';
    }
    html += '</ul></details>';
  }
  html += '</div>';

  section.innerHTML = html;
  section.style.display = 'block';
}


function handleReviewerComplete(msg) {
  // Pro-tier final reviewer (analysis/final_reviewer.py) ran 6 focused
  // calls and returned counts. Show a small banner so the operator knows
  // the holistic pass happened and how many mutations landed.
  var section = document.getElementById('reviewer-section');
  if (!section) return;

  var recal = msg.recalibrations || 0;
  var contra = msg.contradictions || 0;
  var cite = msg.citation_errors || 0;
  var tone = msg.tone_rewrites || 0;
  var systemic = msg.systemic_issues || 0;
  var applied = msg.applied || {};

  var totalIssues = recal + contra + cite + tone;
  var html = '<div class="audit-banner ' +
    (totalIssues > 0 ? 'audit-warns' : 'audit-ok') + '">';
  html += '<strong>Final reviewer (Pro):</strong> ';
  if (totalIssues === 0 && systemic === 0) {
    html += 'no issues flagged. ACR is internally consistent.';
  } else {
    var parts = [];
    if (recal > 0) parts.push(recal + ' verdict recalibration' + (recal === 1 ? '' : 's'));
    if (contra > 0) parts.push(contra + ' cross-SC contradiction' + (contra === 1 ? '' : 's'));
    if (cite > 0) parts.push(cite + ' citation error' + (cite === 1 ? '' : 's'));
    if (tone > 0) parts.push(tone + ' tone rewrite' + (tone === 1 ? '' : 's'));
    if (systemic > 0) parts.push(systemic + ' systemic pattern' + (systemic === 1 ? '' : 's'));
    html += parts.join(', ') + '. ';
    html += 'Applied to ACR: ' + (applied.recalibrated || 0) + ' verdicts, ' +
      (applied.rewritten || 0) + ' rewrites.';
  }
  html += '</div>';
  section.innerHTML = html;
  section.style.display = 'block';
}


function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function updatePhase(phase, totalPages, detailMessage) {
  var badge = document.getElementById('phase-badge');
  var messageEl = document.getElementById('phase-message');
  var section = document.querySelector('.progress-section');

  if (badge) {
    badge.className = 'badge badge-' + phase;
    badge.textContent = phase;
  }

  if (messageEl) {
    var text = _phaseMessages[phase] || 'Processing...';
    if (phase === 'testing' && totalPages && totalPages > 1) {
      text = 'Running accessibility tests across ' + totalPages + ' pages...';
    }
    messageEl.textContent = text;

    // Backend-supplied detail (capture-step telemetry, resume notices,
    // "Testing N linked documents...") — shown under the canned title.
    var detailEl = document.getElementById('phase-detail');
    if (detailMessage) {
      if (!detailEl) {
        detailEl = document.createElement('div');
        detailEl.id = 'phase-detail';
        detailEl.style.fontSize = '.82rem';
        detailEl.style.color = '#616161';
        detailEl.style.marginTop = '.25rem';
        messageEl.parentNode.insertBefore(detailEl, messageEl.nextSibling);
      }
      detailEl.textContent = detailMessage;
    } else if (detailEl) {
      detailEl.textContent = '';
    }
  }

  if (section) {
    section.dataset.initialStatus = phase;
  }
}

function handleCrawlProgress(msg) {
  var el = document.getElementById('crawl-info');
  var msgEl = document.getElementById('crawl-message');
  if (el && msgEl) {
    el.style.display = 'block';
    var text = msg.message || '';
    if (msg.discovered) {
      text += ' (' + msg.discovered + ' pages discovered)';
    }
    msgEl.textContent = text;
  }
}

function handleSitePageStart(msg) {
  var el = document.getElementById('site-page-info');
  var msgEl = document.getElementById('site-page-message');
  if (el && msgEl) {
    el.style.display = 'block';
    var pageNumber = msg.page_number ?? msg.page_num;
    msgEl.textContent = 'Testing page ' + pageNumber + ' of ' + msg.total_pages + ': ' + msg.page_url;
  }
}

function handleSitePageComplete(msg) {
  var el = document.getElementById('site-page-info');
  var msgEl = document.getElementById('site-page-message');
  if (el && msgEl) {
    var pageNumber = msg.page_number ?? msg.page_num;
    var criteriaTested = msg.criteria_tested ?? msg.results_count;
    msgEl.textContent = 'Completed page ' + pageNumber + ' of ' + msg.total_pages +
                        ' (' + (criteriaTested || 0) + ' criteria tested)';
  }
}

function handleSitePageError(msg) {
  var el = document.getElementById('site-page-info');
  var msgEl = document.getElementById('site-page-message');
  if (el && msgEl) {
    el.style.display = 'block';
    el.style.background = '#ffebee';
    el.style.borderColor = '#ef9a9a';
    el.style.color = '#c62828';
    var pageNumber = msg.page_number ?? msg.page_num;
    msgEl.textContent = 'Error on page ' + pageNumber + ' of ' + msg.total_pages +
                        ': ' + (msg.error || 'Unknown error');
  }
}

function handleTestStart(msg) {
  // Update progress bar
  var pct = msg.total > 0 ? Math.round((msg.index / msg.total) * 100) : 0;
  updateProgressBar(pct);

  // Add or update test card
  var grid = document.getElementById('test-cards-grid');
  if (!grid) return;

  var cardId = 'tc-' + msg.criterion_id.replace(/\./g, '-');
  var card = document.getElementById(cardId);

  if (!card) {
    card = document.createElement('div');
    card.id = cardId;
    card.className = 'test-card running';
    card.innerHTML =
      '<div class="criterion-id">' + escapeHtml(msg.criterion_id) + '</div>' +
      '<div class="criterion-name">' + escapeHtml(msg.criterion_name) + '</div>' +
      '<div class="test-status"><span class="spinner"></span>Running...</div>';
    grid.appendChild(card);
  } else {
    card.className = 'test-card running';
    var statusEl = card.querySelector('.test-status');
    if (statusEl) {
      statusEl.innerHTML = '<span class="spinner"></span>Running...';
    }
  }
}

function handleTestComplete(msg) {
  // Update progress bar
  // msg.index is already 1-based (server sends idx+1)
  var pct = msg.total > 0 ? Math.min(Math.round((msg.index / msg.total) * 100), 100) : 100;
  updateProgressBar(pct);

  var grid = document.getElementById('test-cards-grid');
  if (!grid) return;

  var cardId = 'tc-' + msg.criterion_id.replace(/\./g, '-');
  var card = document.getElementById(cardId);

  if (!card) {
    card = document.createElement('div');
    card.id = cardId;
    grid.appendChild(card);
  }

  var confClass = getConfClass(msg.conformance_level);
  card.className = 'test-card complete ' + confClass;

  var section = document.querySelector('.progress-section');
  var reviewId = section ? section.dataset.reviewId : '';
  var findingText = msg.finding_count > 0
    ? msg.finding_count + ' finding' + (msg.finding_count !== 1 ? 's' : '')
    : 'No findings';

  var statusHtml = '<span class="' + getConfTextClass(msg.conformance_level) + '">' +
                   escapeHtml(msg.conformance_level) + '</span>' +
                   '<br><span style="font-size:.75rem;color:#757575;">' + findingText + '</span>';

  if (reviewId) {
    card.innerHTML =
      '<a href="/review/' + reviewId + '/test/' + encodeURIComponent(msg.criterion_id) + '">' +
        '<div class="criterion-id">' + escapeHtml(msg.criterion_id) + '</div>' +
        '<div class="criterion-name">' + escapeHtml(msg.criterion_name) + '</div>' +
        '<div class="test-status">' + statusHtml + '</div>' +
      '</a>';
  } else {
    card.innerHTML =
      '<div class="criterion-id">' + escapeHtml(msg.criterion_id) + '</div>' +
      '<div class="criterion-name">' + escapeHtml(msg.criterion_name) + '</div>' +
      '<div class="test-status">' + statusHtml + '</div>';
  }
}

function handleComplete(msg) {
  updatePhase('complete');
  updateProgressBar(100);

  var summary = msg.summary || {};
  setTextContent('sum-supports', summary.supports || 0);
  setTextContent('sum-partially', summary.partially_supports || 0);
  setTextContent('sum-does-not', summary.does_not_support || 0);
  setTextContent('sum-na', summary.not_applicable || 0);
  setTextContent('sum-ne', summary.not_evaluated || 0);

  renderPerPageBreakdown(msg);

  var completionEl = document.getElementById('completion-summary');
  if (completionEl) {
    completionEl.style.display = 'block';
  }
}

function renderPerPageBreakdown(msg) {
  // Multi-page / site-crawl completions carry pages_tested and
  // per_page_summary ([{url, supports, partially_supports,
  // does_not_support, not_applicable, not_evaluated, total_findings}]).
  var container = document.getElementById('per-page-breakdown');
  if (!container) return;
  var perPage = msg.per_page_summary;
  if (!Array.isArray(perPage) || perPage.length === 0) return;

  var html = '<h4 style="margin:.75rem 0 .35rem;">Per-page results';
  if (msg.pages_tested) html += ' (' + msg.pages_tested + ' pages tested)';
  html += '</h4>';
  html += '<div style="overflow-x:auto;"><table class="results-table" style="font-size:.82rem;">';
  html += '<thead><tr><th scope="col">Page</th><th scope="col">S</th><th scope="col">PS</th>' +
          '<th scope="col">DNS</th><th scope="col">N/A</th><th scope="col">NE</th>' +
          '<th scope="col">Findings</th></tr></thead><tbody>';
  perPage.forEach(function(p) {
    html += '<tr>' +
      '<td style="word-break:break-all;">' + escapeHtml(p.url || '') + '</td>' +
      '<td>' + (p.supports || 0) + '</td>' +
      '<td>' + (p.partially_supports || 0) + '</td>' +
      '<td>' + (p.does_not_support || 0) + '</td>' +
      '<td>' + (p.not_applicable || 0) + '</td>' +
      '<td>' + (p.not_evaluated || 0) + '</td>' +
      '<td>' + (p.total_findings || 0) + '</td>' +
      '</tr>';
  });
  html += '</tbody></table></div>';
  container.innerHTML = html;
  container.style.display = 'block';
}

function handleError(msg) {
  updatePhase('error');

  var errorDisplay = document.getElementById('error-display');
  var errorMessage = document.getElementById('error-message');
  if (errorDisplay && errorMessage) {
    errorDisplay.style.display = 'block';
    errorMessage.textContent = msg.message || 'An unexpected error occurred.';
  }
}

function updateProgressBar(pct) {
  var fill = document.getElementById('progress-fill');
  var track = document.getElementById('progress-track');
  if (fill) {
    fill.style.width = pct + '%';
  }
  if (track) {
    track.setAttribute('aria-valuenow', String(pct));
  }
}

/* ------------------------------------------------------------------
   Finding decision buttons
   ------------------------------------------------------------------ */
function filterFindings() {
  // Client-side filter for the Findings list on test_detail.html. Filters
  // by free-text search, severity, and source. Shown count updates live.
  var q = (document.getElementById('finding-search') || {}).value || '';
  var sev = (document.getElementById('severity-filter') || {}).value || '';
  var src = (document.getElementById('source-filter') || {}).value || '';
  var qLower = q.toLowerCase().trim();

  var cards = document.querySelectorAll('.finding-card');
  var visible = 0;
  for (var i = 0; i < cards.length; i++) {
    var card = cards[i];
    var sevMatch = !sev || card.classList.contains('severity-' + sev);
    var srcBadge = card.querySelector('.source-badge');
    var srcText = srcBadge ? (srcBadge.textContent || '').trim() : '';
    // Sources can be comma-joined ("axe, htmlcs") — match if any tag equals
    // the selected filter value.
    var srcTags = srcText.split(',').map(function(t) { return t.trim().toLowerCase(); });
    var srcMatch = !src || srcTags.indexOf(src.toLowerCase()) !== -1;
    var textMatch = !qLower || (card.textContent || '').toLowerCase().indexOf(qLower) !== -1;
    var show = sevMatch && srcMatch && textMatch;
    card.style.display = show ? '' : 'none';
    if (show) visible++;
  }
  var counter = document.getElementById('finding-count');
  if (counter) {
    counter.textContent = visible + ' of ' + cards.length + ' shown';
  }
}


function setDecision(reviewId, criterionId, findingId, status, btn) {
  var url = '/api/review/' + reviewId + '/test/' + encodeURIComponent(criterionId) +
            '/finding/' + findingId + '/decision';

  var reason = '';
  if (status === 'accepted' || status === 'rejected') {
    var input = window.prompt(
      'Optional reason for marking this finding as ' + status + ' (leave blank to skip):', '');
    if (input === null) return; // user cancelled — don't change the decision
    reason = input.trim();
  }

  fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status: status, reason: reason })
  })
    .then(function(r) {
      if (!r.ok) throw new Error('Decision update failed');
      return r.json();
    })
    .then(function() {
      // Update button states
      var card = btn.closest('.finding-card');
      if (!card) return;

      var buttons = card.querySelectorAll('.decision-btn');
      buttons.forEach(function(b) { b.classList.remove('active'); });

      if (status === 'accepted') {
        card.querySelector('.decision-btn.accept').classList.add('active');
      } else if (status === 'rejected') {
        card.querySelector('.decision-btn.reject').classList.add('active');
      }

      // Update decision badge in header
      var header = card.querySelector('.finding-header');
      var existingBadge = header.querySelector('.decision-badge');
      if (existingBadge) existingBadge.remove();

      if (status !== 'undecided') {
        var badge = document.createElement('span');
        badge.className = 'decision-badge ' + status;
        badge.textContent = status;
        header.appendChild(badge);
      }

      // Update the displayed decision reason
      var reasonEl = card.querySelector('.decision-reason');
      if (reason && status !== 'undecided') {
        if (!reasonEl) {
          reasonEl = document.createElement('div');
          reasonEl.className = 'finding-detail mt-1 decision-reason';
          var btnRow = card.querySelector('.decision-buttons');
          card.insertBefore(reasonEl, btnRow);
        }
        reasonEl.innerHTML = '<strong>Decision Reason:</strong> ' + escapeHtml(reason);
      } else if (reasonEl) {
        reasonEl.remove();
      }
    })
    .catch(function(err) {
      alert('Could not update decision: ' + err.message);
    });
}

/* ------------------------------------------------------------------
   Helpers
   ------------------------------------------------------------------ */
function escapeHtml(str) {
  if (!str) return '';
  var div = document.createElement('div');
  div.appendChild(document.createTextNode(str));
  return div.innerHTML;
}

function setTextContent(id, text) {
  var el = document.getElementById(id);
  if (el) el.textContent = String(text);
}

function getConfClass(conformanceLevel) {
  if (!conformanceLevel) return 'conf-ne';
  var cl = conformanceLevel.toLowerCase();
  if (cl === 'supports') return 'conf-pass';
  if (cl === 'partially supports') return 'conf-partial';
  if (cl === 'does not support') return 'conf-fail';
  if (cl === 'not applicable') return 'conf-na';
  return 'conf-ne';
}

function getConfTextClass(conformanceLevel) {
  if (!conformanceLevel) return 'conf-not-evaluated';
  var cl = conformanceLevel.toLowerCase();
  if (cl === 'supports') return 'conf-supports';
  if (cl === 'partially supports') return 'conf-partially-supports';
  if (cl === 'does not support') return 'conf-does-not-support';
  if (cl === 'not applicable') return 'conf-not-applicable';
  return 'conf-not-evaluated';
}
