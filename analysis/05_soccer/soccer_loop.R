# The soccer analysis loop: does Elo-vs-DraftKings 3-way earn anything, and are the
# soccer constants in soccer_edge.py the right priors?
#
#   A. Paper ROI by pick x strength, flat $1, bootstrap 95% CI.
#   B. Slices a rule can act on: edge band, price band, pick (Wilson CI on hit, flat ROI).
#   C. Elo calibration on settled snapshots + 3-way log-loss of the model vs the closer.
#   D. Refit on the results table: replay Elo for a grid of ELO_HFA, score every DRAW_BASE
#      by 3-way log-likelihood on the held-out second half.
#
# Output: analysis/_out/soccer_*.csv. Mirrors soccer_loop.py — keep in lockstep.

suppressPackageStartupMessages({
  library(dplyr)
  library(boot)
})

.this_dir <- tryCatch(dirname(sys.frame(1)$ofile), error = function(e) "analysis/05_soccer")
source(file.path(.this_dir, "..", "_shared", "load_data.R"))     # wilson()
source(file.path(.this_dir, "..", "_shared", "load_soccer.R"))

OUT_DIR   <- normalizePath(file.path(.this_dir, "..", "_out"), mustWork = FALSE)
MIN_BETS  <- 10
MIN_N     <- 5
BOOT_REPS <- 5000
SEED      <- 20260920

ELO_START <- 1500; ELO_K <- 20; ELO_K_FRIENDLY <- 10
ELO_HFA_LIVE <- 60; DRAW_BASE_LIVE <- 0.26
HFA_GRID  <- c(0, 30, 60, 90, 120)
DRAW_GRID <- c(0.20, 0.22, 0.24, 0.26, 0.28, 0.30, 0.32)
HOLDOUT_FRAC <- 0.5

EDGE_BINS  <- c(0, 8, 15, 20, 30, 50, 100000)
EDGE_LABELS <- c("0-8", "8-15", "15-20", "20-30", "30-50", "50+")
PRICE_BINS <- c(-100000, -200, -110, 99, 150, 200, 250, 100000)
PRICE_LABELS <- c("<-200", "-200..-110", "-110..+100", "+100..150", "+150..200", "+200..250", ">+250")
PROB_BINS  <- c(0, .2, .3, .4, .5, .6, .7, .8, 1.01)
PROB_LABELS <- c("<.2", ".2-.3", ".3-.4", ".4-.5", ".5-.6", ".6-.7", ".7-.8", ".8+")

dir.create(OUT_DIR, showWarnings = FALSE, recursive = TRUE)
set.seed(SEED)

verdict <- function(lo, hi) if (lo > 0) "PROFITABLE (95% CI > 0)" else if (hi < 0) "losing (95% CI < 0)" else "inconclusive"
boot_ci <- function(x) {
  b <- boot(x, function(d, i) mean(d[i]), R = BOOT_REPS)
  ci <- boot.ci(b, type = "perc")$percent
  c(lo = ci[4], hi = ci[5])
}

# ---------------------------------------------------------------- A
section_a <- function(bets) {
  cat(sprintf("A. Paper ROI — %s settled soccer paper bets, backfilled %s, flat ROI %+.1f%%\n\n",
              format(nrow(bets), big.mark = ","), format(sum(bets$backfill), big.mark = ","), 100 * mean(bets$pnl_flat)))
  groups <- list(list(pick = "ALL", strength = "all", g = bets))
  for (key in split(bets, list(bets$pick, bets$strength), drop = TRUE))
    groups[[length(groups) + 1]] <- list(pick = key$pick[1], strength = as.character(key$strength[1]), g = key)
  for (key in split(bets, bets$pick))
    groups[[length(groups) + 1]] <- list(pick = key$pick[1], strength = "any", g = key)
  for (key in split(bets, bets$strength))
    groups[[length(groups) + 1]] <- list(pick = "ALL", strength = as.character(key$strength[1]), g = key)
  rows <- lapply(groups, function(x) {
    g <- x$g
    if (nrow(g) < MIN_BETS) return(NULL)
    ci <- boot_ci(g$pnl_flat)
    data.frame(pick = x$pick, strength = x$strength, bets = nrow(g), wins = sum(g$result == "W"),
               hit_rate = mean(g$result == "W"), roi_mean = mean(g$pnl_flat),
               roi_ci_lo = ci[["lo"]], roi_ci_hi = ci[["hi"]], verdict = verdict(ci[["lo"]], ci[["hi"]]),
               stringsAsFactors = FALSE)
  })
  out <- bind_rows(rows) %>% arrange(desc(roi_ci_lo))
  cat(sprintf("  %-7s%4s%6s%6s%7s%8s%8s%8s  verdict\n", "pick", "str", "bets", "wins", "hit%", "ROI", "CI lo", "CI hi"))
  for (i in seq_len(nrow(out))) {
    r <- out[i, ]
    cat(sprintf("  %-7s%4s%6d%6d%6.1f%%%+7.1f%%%+7.1f%%%+7.1f%%  %s\n", r$pick, r$strength, r$bets, r$wins,
                100 * r$hit_rate, 100 * r$roi_mean, 100 * r$roi_ci_lo, 100 * r$roi_ci_hi, r$verdict))
  }
  out
}

# ---------------------------------------------------------------- B
slice_table <- function(df, col, order, title) {
  cat(sprintf("\n%s\n", title))
  cat(sprintf("  %-12s%5s%5s%8s%8s   95%% CI on hit\n", "bucket", "n", "W", "hit%", "ROI"))
  rows <- list()
  for (key in order[order %in% unique(df[[col]])]) {
    g <- df[df[[col]] == key, ]
    if (nrow(g) < MIN_N) next
    w <- sum(g$result == "W"); wi <- wilson(w, nrow(g))
    rows[[length(rows) + 1]] <- data.frame(dimension = col, bucket = key, n = nrow(g), wins = w, hit_rate = wi[1],
                                           hit_lo = wi[2], hit_hi = wi[3], roi_flat = mean(g$pnl_flat),
                                           stringsAsFactors = FALSE)
    cat(sprintf("  %-12s%5d%5d%7.1f%%%+7.1f%%   [%.0f, %.0f]\n", key, nrow(g), w, 100 * wi[1],
                100 * mean(g$pnl_flat), 100 * wi[2], 100 * wi[3]))
  }
  rows
}

section_b <- function(bets) {
  b <- bets[bets$strength >= 1, ]
  b$edge_band  <- as.character(cut(b$edge, EDGE_BINS, labels = EDGE_LABELS, right = FALSE))
  b$price_band <- as.character(cut(b$price, PRICE_BINS, labels = PRICE_LABELS, right = TRUE))
  rows <- slice_table(b, "edge_band", EDGE_LABELS, "B1. by edge band (does a bigger Elo-vs-DK gap win more?)")
  rows <- c(rows, slice_table(b, "price_band", PRICE_LABELS, "B2. by price band"))
  rows <- c(rows, slice_table(b, "pick", c("home", "draw", "away"), "B3. by pick"))
  bind_rows(rows)
}

# ---------------------------------------------------------------- C
section_c <- function(m) {
  cat(sprintf("\nC. Elo calibration on %s settled matches with a closer and a rating on both sides\n",
              format(nrow(m), big.mark = ",")))
  rows <- list()
  for (spec in list(c("home_p", "P(home win)", "home"), c("draw_p", "P(draw)", "draw"), c("away_p", "P(away win)", "away"))) {
    col <- spec[1]; hit <- spec[3]
    cat(sprintf("  %s: model bin -> observed rate (Wilson 95%%)\n", spec[2]))
    band <- as.character(cut(m[[col]], PROB_BINS, labels = PROB_LABELS, right = FALSE))
    for (key in PROB_LABELS[PROB_LABELS %in% unique(band)]) {
      g <- m[band == key, ]
      if (nrow(g) < MIN_N) next
      k <- sum(g$outcome == hit); wi <- wilson(k, nrow(g)); pred <- mean(g[[col]])
      rows[[length(rows) + 1]] <- data.frame(outcome = hit, bin = key, n = nrow(g), pred = pred, obs = wi[1],
                                             obs_lo = wi[2], obs_hi = wi[3], stringsAsFactors = FALSE)
      cat(sprintf("    %-7s n=%5d  pred %5.1f%%  obs %5.1f%% [%5.1f,%5.1f]  Δ%+5.1fpp\n", key, nrow(g),
                  100 * pred, 100 * wi[1], 100 * wi[2], 100 * wi[3], 100 * (wi[1] - pred)))
    }
  }
  eps <- 1e-9
  mp <- ifelse(m$outcome == "home", m$home_p, ifelse(m$outcome == "draw", m$draw_p, m$away_p))
  fp <- ifelse(m$outcome == "home", m$fair_home, ifelse(m$outcome == "draw", m$fair_draw, m$fair_away))
  ll_model <- -mean(log(pmin(pmax(mp, eps), 1))); ll_market <- -mean(log(pmin(pmax(fp, eps), 1)))
  cat(sprintf("  3-way log-loss (lower is better): Elo model %.4f · de-vigged closer %.4f → %s by %.4f\n",
              ll_model, ll_market, if (ll_market < ll_model) "the closer is sharper" else "the model is sharper",
              abs(ll_model - ll_market)))
  rows[[length(rows) + 1]] <- data.frame(outcome = "logloss", bin = "model", n = nrow(m), pred = ll_model,
                                         obs = ll_market, obs_lo = NA, obs_hi = NA, stringsAsFactors = FALSE)
  bind_rows(rows)
}

# ---------------------------------------------------------------- D
replay <- function(res, hfa) {
  r <- new.env(hash = TRUE)
  n <- nrow(res); e_out <- numeric(n)
  k_col <- ifelse(grepl("friendly", res$league), ELO_K_FRIENDLY, ELO_K)
  h <- res$home_id; a <- res$away_id; hs <- res$home_score; as_ <- res$away_score; neu <- res$neutral
  for (i in seq_len(n)) {
    rh <- if (is.null(r[[h[i]]])) ELO_START else r[[h[i]]]
    ra <- if (is.null(r[[a[i]]])) ELO_START else r[[a[i]]]
    e <- 1 / (1 + 10 ^ (-(rh - ra + (if (neu[i]) 0 else hfa)) / 400))
    e_out[i] <- e
    s <- if (hs[i] > as_[i]) 1 else if (hs[i] < as_[i]) 0 else 0.5
    gd <- abs(hs[i] - as_[i])
    mult <- if (gd <= 1) 1 else if (gd == 2) 1.5 else (11 + gd) / 8
    d <- k_col[i] * mult * (s - e)
    r[[h[i]]] <- rh + d; r[[a[i]]] <- ra - d
  }
  e_out
}

three_way <- function(e, draw_base) {
  pd <- draw_base * 4 * e * (1 - e)
  ph <- pmax(0, e - pd / 2); pa <- pmax(0, 1 - e - pd / 2)
  s <- ph + pd + pa
  list(ph = ph / s, pd = pd / s, pa = pa / s)
}

section_d <- function(res) {
  n <- nrow(res); start <- as.integer(n * HOLDOUT_FRAC)
  idx <- (start + 1):n
  out <- ifelse(res$home_score > res$away_score, "home", ifelse(res$home_score < res$away_score, "away", "draw"))[idx]
  cat(sprintf("\nD. Refit ELO_HFA x DRAW_BASE on the results table — %s matches, scoring the last %s (3-way log-likelihood per match, higher is better)\n",
              format(n, big.mark = ","), format(n - start, big.mark = ",")))
  rows <- list()
  for (hfa in HFA_GRID) {
    e <- replay(res, hfa)[idx]
    for (db in DRAW_GRID) {
      tw <- three_way(e, db)
      p <- ifelse(out == "home", tw$ph, ifelse(out == "draw", tw$pd, tw$pa))
      rows[[length(rows) + 1]] <- data.frame(hfa = hfa, draw_base = db, loglik = mean(log(pmin(pmax(p, 1e-9), 1))))
    }
  }
  fit <- bind_rows(rows)
  best <- fit[which.max(fit$loglik), ]
  live <- fit[fit$hfa == ELO_HFA_LIVE & fit$draw_base == DRAW_BASE_LIVE, ]
  cat(sprintf("  %5s  %s   (DRAW_BASE)\n", "HFA", paste(sprintf("%8.2f", DRAW_GRID), collapse = "")))
  for (hfa in HFA_GRID) {
    row <- fit[fit$hfa == hfa, ] %>% arrange(draw_base)
    cat(sprintf("  %5.0f  %s\n", hfa, paste(sprintf("%8.4f", row$loglik), collapse = "")))
  }
  cat(sprintf("  best: HFA %.0f, DRAW_BASE %.2f (loglik %.4f); live: HFA %.0f, DRAW_BASE %.2f (loglik %.4f); gap %.4f per match\n",
              best$hfa, best$draw_base, best$loglik, ELO_HFA_LIVE, DRAW_BASE_LIVE, live$loglik, best$loglik - live$loglik))
  fit
}

bets <- load_soccer_bets()
if (nrow(bets) == 0) {
  cat("no settled soccer paper bets yet — run soccer_edge.py --snapshot / --settle (or --backfill)\n")
  quit(status = 0)
}
write.csv(section_a(bets), file.path(OUT_DIR, "soccer_roi.csv"), row.names = FALSE)
write.csv(section_b(bets), file.path(OUT_DIR, "soccer_slices.csv"), row.names = FALSE)
m <- load_soccer_matches()
if (nrow(m) > 0) write.csv(section_c(m), file.path(OUT_DIR, "soccer_calibration.csv"), row.names = FALSE)
res <- load_results()
if (nrow(res) >= 200) write.csv(section_d(res), file.path(OUT_DIR, "soccer_fit.csv"), row.names = FALSE)
cat(sprintf("\nwrote %s\n", file.path(OUT_DIR, "soccer_*.csv")))
