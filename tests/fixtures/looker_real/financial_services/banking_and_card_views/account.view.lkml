view: account {
  sql_table_name: `looker-private-demo.retail_banking.account` ;;
  drill_fields: [account_id]

  dimension: account_id {
    primary_key: yes
    type: number
    sql: ${TABLE}.account_id ;;
    value_format_name: id
    drill_fields: []
    action: {
      label: "Email Client About Investment Meeting"
      url: "https://desolate-refuge-53336.herokuapp.com/posts"
      icon_url: "https://sendgrid.com/favicon.ico"
      param: {
        name: "some_auth_code"
        value: "abc123456"
      }
      form_param: {
        name: "Subject"
        required: yes
        default: "Investment Options for you, {{ client.first_name._value }}"
      }
      form_param: {
        name: "Body"
        type: textarea
        required: yes
        default:
        "Dear {{ client.first_name._value }},

        Thanks for your loyalty to the Look.  Based on recent activity with your account, we wanted to offer you the opportunity to schedule an appointment with one of our investment advisors who can make sure you're making the most of your money.

        Your friends at the Look"
      }
    }
    action: {
      label: "Send Client Savings Account Details"
      url: "https://desolate-refuge-53336.herokuapp.com/posts"
      icon_url: "https://image.flaticon.com/icons/svg/1611/1611179.svg"
      param: {
        name: "some_auth_code"
        value: "abc123456"
      }
      form_param: {
        name: "Subject"
        required: yes
        default: "Saving for your Future, {{ client.first_name._value }}"
      }
      form_param: {
        name: "Body"
        type: textarea
        required: yes
        default:
        "Dear {{ client.first_name._value }},

        Thanks for your loyalty to the Look.  Based on recent activity with your account, we wanted to recommend that you investigate our new High Yield Savings Account Offerings, which you can find attached here.

        Your friends at the Look"
      }
    }
    link: {
      label: "Customer Account Overview"
      url: "/dashboards/goGpXFSyjbvdcrPMNAalS8?Account%20ID={{ value | encode_uri }}"
      icon_url: "http://www.looker.com/favicon.ico"
    }
    required_fields: [client.first_name]
  }

  dimension_group: create {
    type: time
    timeframes: [date, day_of_month, day_of_year, week, month, month_name, quarter, quarter_of_year, year, raw]
#     datatype: yyyymmdd
    sql: timestamp(${TABLE}.date) ;;
  }

  dimension: district_id {
    label: "Branch ID"
    description: "ID of the branch where the account was created"
    type: number
    sql: ${TABLE}.district_id ;;
    value_format_name: id
  }

  dimension: account_manager_id {
    type: string
    sql: concat('001',${district_id});;
    action: {
      label: "Send Message to Account Manager"
      url: "https://hooks.zapier.com/hooks/catch/1662138/tvc3zj/"
      param: {
        name: "user_dash_link"
        value: "/dashboards/goGpXFSyjbvdcrPMNAalS8?Account%20ID={{ account_id._value }}"
      }
      form_param: {
        name: "Message"
        type: textarea
        default: "Hey,
        Based on recent activity in this account, account #{{ account_id._value }}, I think you may want to schedule a call with the client.

        ~{{ _user_attributes.first_name}}"
      }
      form_param: {
        name: "Recipient"
        type: select
        default: "zevl"
        option: {
          name: "zevl"
          label: "Zev"
        }
        option: {
          name: "slackdemo"
          label: "Slack Demo User"
        }
      }
      form_param: {
        name: "Channel"
        type: select
        default: "cs"
        option: {
          name: "cs"
          label: "Customer Support"
        }
        option: {
          name: "general"
          label: "General"
        }
      }
    }
  }

  dimension: stmt_frq {
    label: "Statement Frequency"
    description: "frequency of issuance of statements"
    type: string
#     sql: ${TABLE}.stmt_frq ;;
    case: {
      when: {
        label: "Weekly"
        sql: ${TABLE}.stmt_frq = 'weekly' ;;
      }
      when: {
        label: "Monthly"
        sql: ${TABLE}.stmt_frq = 'monthly' ;;
      }
      when: {
        label: "After Transaction"
        sql: ${TABLE}.stmt_frq = 'after_tr' ;;
      }
    }
  }

  measure: number_of_accounts {
    type: count
    drill_fields: [detail*]
  }

  # ----- Sets of fields for drilling ------
  set: detail {
    fields: [
      account_id,
      district.district_id,
      district.dname,
      disp.count,
      trans.count,
      order.count,
      loan.count
    ]
  }
}
