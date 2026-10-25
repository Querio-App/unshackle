# Paste at the bottom of the model file, next to an include of the round 2 view file.

explore: ev_f {}

# explore-level filters on a renamed view: access_filter reads the user's locale ('en')
explore: ev_af {
  from: ev_f
  sql_always_where: ${ev_af.n} > 0 ;;
  always_filter: { filters: [ev_af.y: "yes"] }
  access_filter: { field: ev_af.s user_attribute: locale }
}
