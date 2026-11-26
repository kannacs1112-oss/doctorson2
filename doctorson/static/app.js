(function () {
  "use strict";

  function formatStamp(tz) {
    var now = new Date();
    try {
      var fmt = new Intl.DateTimeFormat("en-GB", {
        weekday: "short",
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
        hour12: true,
        timeZone: tz || undefined
      });
      return "Live · " + fmt.format(now) + (tz ? " (" + tz + ")" : "");
    } catch (_e) {
      return "Live · " + now.toLocaleTimeString();
    }
  }

  function setOnline(dot, stamp, tz) {
    if (dot) dot.classList.remove("stale");
    if (stamp) {
      stamp.classList.remove("stale");
      stamp.textContent = formatStamp(tz || stamp.dataset.tz || "");
    }
  }

  function setOffline(dot, stamp) {
    if (dot) dot.classList.add("stale");
    if (stamp) {
      stamp.classList.add("stale");
      stamp.textContent = "Offline - retrying...";
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    // 1. Safe confirmation prompts via data-confirm
    document.addEventListener("submit", function (e) {
      var form = e.target;
      if (form && form.dataset && form.dataset.confirm) {
        if (!window.confirm(form.dataset.confirm)) {
          e.preventDefault();
        }
      }
    });

    // 2. Auto-submit filter dropdowns and checkboxes
    var filterForms = document.querySelectorAll("form[data-auto-submit]");
    filterForms.forEach(function (form) {
      form.addEventListener("change", function (e) {
        if (e.target && (e.target.tagName === "SELECT" || e.target.type === "checkbox")) {
          form.submit();
        }
      });
    });

    var dot = document.querySelector(".live-bar .dot");
    var stamp = document.getElementById("live-stamp");

    // 3. Live polling for main doctor list or SOS emergency list
    var liveContainer = document.querySelector("[data-live-url]");
    if (liveContainer) {
      var liveUrl = liveContainer.dataset.liveUrl;
      var intervalMs = parseInt(liveContainer.dataset.liveInterval || "30000", 10);

      window.setInterval(function () {
        if (document.hidden) return;
        fetch(liveUrl + window.location.search, { headers: { "X-Requested-With": "fetch" } })
          .then(function (resp) {
            if (!resp.ok) throw new Error("HTTP " + resp.status);
            return resp.text();
          })
          .then(function (html) {
            liveContainer.innerHTML = html;
            setOnline(dot, stamp);
          })
          .catch(function () {
            setOffline(dot, stamp);
          });
      }, intervalMs);
    }

    // 4. AI prescription extraction. The server owns all eligibility decisions.
    var rxForm = document.getElementById("pharmacy-checkout-form");
    var rxFile = document.getElementById("rx-prescription-file");
    var rxButton = document.getElementById("rx-analyze-button");
    var rxResult = document.getElementById("rx-analysis-result");
    var rxToken = document.getElementById("rx-upload-token");
    var rxConsent = document.getElementById("rx-ai-consent");
    var rxCartSummary = document.getElementById("pharmacy-cart-summary");
    var rxDiscountPreview = document.getElementById("rx-discount-preview");
    var rxDiscountAmount = document.getElementById("rx-discount-amount");
    var cartTotalValue = document.getElementById("cart-total-value");

    function clearRxDiscountPreview() {
      if (rxDiscountPreview) rxDiscountPreview.hidden = true;
      if (rxCartSummary && cartTotalValue) {
        var base = Number(rxCartSummary.dataset.subtotal || 0) + Number(rxCartSummary.dataset.deliveryFee || 0);
        cartTotalValue.textContent = (rxCartSummary.dataset.currency || "Rs.") + " " + base.toFixed(2);
      }
    }

    function setRxDiscountPreview(percent) {
      if (!rxCartSummary || !rxDiscountPreview || !rxDiscountAmount || !cartTotalValue) return;
      var subtotal = Number(rxCartSummary.dataset.subtotal || 0);
      var deliveryFee = Number(rxCartSummary.dataset.deliveryFee || 0);
      var discount = Math.round((subtotal * Number(percent) / 100 + Number.EPSILON) * 100) / 100;
      var currency = rxCartSummary.dataset.currency || "Rs.";
      rxDiscountAmount.textContent = "−" + currency + " " + discount.toFixed(2);
      cartTotalValue.textContent = currency + " " + (subtotal - discount + deliveryFee).toFixed(2);
      rxDiscountPreview.hidden = false;
    }

    function showRxResult(message, className) {
      if (!rxResult) return;
      rxResult.hidden = false;
      rxResult.className = "rx-analysis-result " + (className || "warning");
      rxResult.textContent = message;
    }

    if (rxFile && rxToken) {
      rxFile.addEventListener("change", function () {
        rxToken.value = "";
        if (rxResult) {
          rxResult.hidden = true;
          rxResult.textContent = "";
        }
        clearRxDiscountPreview();
      });
    }

    if (rxButton && rxForm && rxFile && rxResult && rxToken) {
      rxButton.addEventListener("click", function () {
        if (!rxFile.files || !rxFile.files[0]) {
          showRxResult("Choose a prescription photo or PDF first.", "warning");
          return;
        }
        var data = new FormData();
        data.append("prescription_file", rxFile.files[0]);
        var csrf = rxForm.querySelector('input[name="csrf"]');
        if (csrf) data.append("csrf", csrf.value);
        if (rxConsent && rxConsent.checked) data.append("rx_ai_consent", "1");
        rxButton.disabled = true;
        rxButton.textContent = "Analysing securely…";
        showRxResult("Uploading the prescription for extraction…", "warning");

        fetch(rxForm.dataset.rxAnalyzeUrl, { method: "POST", body: data })
          .then(function (response) {
            return response.json().then(function (payload) {
              if (!response.ok || !payload.ok) throw new Error(payload.message || "Could not analyse this file.");
              return payload;
            });
          })
          .then(function (payload) {
            rxToken.value = payload.token;
            var lines = [];
            lines.push("Doctor read: " + payload.doctor_name);
            if (payload.matched_doctor_name) lines.push("Matched app doctor: " + payload.matched_doctor_name);
            lines.push("Doctor enrolment: " + ({
              enrolled: "Verified as enrolled for pharmacy offers",
              listed_not_enrolled_for_offer: "Listed in DoctorsOn, but not marked eligible for pharmacy offers",
              not_in_doctorson: "No exact match in the DoctorsOn doctor list",
              not_readable: "Could not reliably read the doctor's name"
            }[payload.doctor_status] || payload.doctor_status));
            lines.push("Document legibility: " + (payload.readable ? "Readable" : "Some details may be unclear"));
            lines.push("Prescription date: " + payload.prescription_date);
            lines.push("Date rule: " + ({
              order_date: "Matches today's India date",
              previous_day: "Matches yesterday's India date",
              future_date: "Future date — not eligible",
              too_old: "Older than one day — not eligible",
              unreadable: "Date could not be verified"
            }[payload.date_status] || payload.date_status));
            if (payload.medicines && payload.medicines.length) {
              lines.push("Medicines read:");
              payload.medicines.forEach(function (m) {
                var detail = [m.name, m.strength, m.directions_as_written].filter(Boolean).join(" · ");
                if (detail) lines.push("  • " + detail);
              });
            }
            if (payload.discount_eligible) {
              lines.push("Eligible for up to " + payload.discount_percent + "% off the medicine subtotal.");
              setRxDiscountPreview(payload.discount_percent);
            } else {
              clearRxDiscountPreview();
              lines.push("No automatic offer. The pharmacist can still review the attached prescription.");
            }
            if (payload.message) lines.push("Note: " + payload.message);
            lines.push("Final dispensing approval remains with the pharmacist.");
            showRxResult(lines.join("\n"), payload.discount_eligible ? "success" : "warning");
            rxResult.style.whiteSpace = "pre-line";
          })
          .catch(function (error) {
            rxToken.value = "";
            clearRxDiscountPreview();
            showRxResult(error.message || "Prescription analysis failed. Please retry.", "error");
          })
          .finally(function () {
            rxButton.disabled = false;
            rxButton.textContent = "Upload & analyse prescription";
          });
      });
    }

    // 5. Live polling on individual doctor profile page
    var profile = document.querySelector("section.profile[data-doctor-id]");
    if (profile) {
      var doctorId = profile.dataset.doctorId;
      var badge = document.getElementById("status-badge");
      var detail = document.getElementById("status-detail");

      window.setInterval(function () {
        if (document.hidden) return;
        fetch("/api/doctors/" + encodeURIComponent(doctorId))
          .then(function (resp) {
            if (!resp.ok) throw new Error("HTTP " + resp.status);
            return resp.json();
          })
          .then(function (payload) {
            if (!payload || !payload.doctor) return;
            var d = payload.doctor;
            if (badge) {
              badge.className = "badge " + d.status;
              badge.textContent = d.status_label;
            }
            if (detail) {
              detail.textContent = d.status_detail || "";
            }
            setOnline(dot, stamp);
          })
          .catch(function () {
            setOffline(dot, stamp);
          });
      }, 30000);
    }
  });
})();
