include: "/views/refined/users_rfn.view.lkml"

explore: users {
  label: "Users"
  description: "Use this Explore to analyze users details."
  view_name: users

  fields: [users.allowed_fields*, -users.password_hash]
}
