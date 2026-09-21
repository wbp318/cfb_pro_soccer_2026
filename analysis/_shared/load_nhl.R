# Shared loader for the NHL half of analysis/ (reads nhl.db, never writes).
#
#   source("analysis/_shared/load_nhl.R")
#   bets <- load_nhl_bets()          # one row per settled NHL paper prop
#   logs <- load_game_logs(season)   # every stored game-log row for a season, chronological
#
# Mirrors load_nhl.py — keep them in lockstep. CFB_NHL_DB overrides the path (CI).

suppressPackageStartupMessages({
  library(DBI)
  library(RSQLite)
})

.nhl_shared_dir <- tryCatch(dirname(sys.frame(1)$ofile), error = function(e) "analysis/_shared")
NHL_DB <- if (nzchar(Sys.getenv("CFB_NHL_DB"))) Sys.getenv("CFB_NHL_DB") else
  normalizePath(file.path(.nhl_shared_dir, "..", "..", "nhl.db"), mustWork = FALSE)

.NBETS_SQL <- "
SELECT p.id, p.game_id, g.date, p.player_id, pl.name, pl.position, p.market, p.side, p.line, p.price,
       p.truth_p, p.edge, p.strength, p.stake, p.book, p.actual, p.result, p.profit
FROM paper_bets p
JOIN games g ON g.id = p.game_id
LEFT JOIN players pl ON pl.id = p.player_id
WHERE p.result IS NOT NULL
"

.NLOGS_SQL <- "
SELECT game_id, player_id, season, date, team, opp, home, shots, points, goals, assists, blocked,
       pp_points, saves, shots_against, started, toi
FROM game_logs WHERE season = ? ORDER BY player_id, date, game_id
"

.nquery <- function(sql, db_path, params = NULL) {
  con <- dbConnect(SQLite(), db_path)
  on.exit(dbDisconnect(con))
  if (is.null(params)) dbGetQuery(con, sql) else dbGetQuery(con, sql, params = params)
}

.ndec <- function(price) ifelse(price > 0, 1 + price / 100, 1 + 100 / abs(price))

load_nhl_bets <- function(db_path = NHL_DB) {
  df <- .nquery(.NBETS_SQL, db_path)
  df$pnl_flat <- ifelse(df$result == "W", .ndec(df$price) - 1, ifelse(df$result == "L", -1, 0))
  df
}

load_game_logs <- function(season, db_path = NHL_DB) .nquery(.NLOGS_SQL, db_path, list(as.integer(season)))
