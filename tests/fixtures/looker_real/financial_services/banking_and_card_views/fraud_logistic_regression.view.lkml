include: "/models/retail_banking_explores.lkml"

######################## TRAINING/TESTING INPUTS #############################

#train / test on older confirmed data (assume that all transactions that occured in the past 2 weeks have not been confirmed)

# we need to balance the training dataset so we are only gonna take about ~1000 rows from the fraud when training
view: fraud_model_training {
  derived_table: {
    sql_trigger_value: select max(trans_date) from `looker-private-demo.retail_banking.card_transactions`;;
    explore_source: card_transactions {
      column: trans_id {}
      column: amount {}
      column: transaction_distance {}
      column: transaction_hour_of_day {}
      column: difference_from_average_distance {}
      column: percent_diff_from_cat {}
      column: is_fraud {}
      filters: {
        field: card_transactions.transaction_date
        value: "before 120 days ago"
      }
    }
  }
}


view: fraud_model_testing {
  derived_table: {
    sql_trigger_value: select max(trans_date) from `looker-private-demo.retail_banking.card_transactions`;;
    explore_source: card_transactions {
      column: trans_id {}
      column: amount {}
      column: transaction_distance {}
      column: transaction_hour_of_day {}
      column: difference_from_average_distance {}
      column: percent_diff_from_cat {}
      column: is_fraud {}
      filters: {
        field: card_transactions.transaction_date
        value: "before 14 days ago,after 120 days ago"
      }
    }
  }
}


######################## MODEL #############################

view: fraud_model {
  derived_table: {
    sql_trigger_value: select max(trans_date) from `looker-private-demo.retail_banking.card_transactions`;;
    sql_create:
      CREATE OR REPLACE MODEL ${SQL_TABLE_NAME}
      OPTIONS(model_type='logistic_reg'
        , labels=['is_fraud']
        ) AS
      SELECT
         * EXCEPT(trans_id)
      FROM ${fraud_model_training.SQL_TABLE_NAME}
      where is_fraud is True or
      (is_fraud is False and rand() <= (
        select sum(case when is_fraud is True then 1 else 0 end)/count(*) from ${fraud_model_training.SQL_TABLE_NAME}));;
  }
}

######################## TRAINING INFORMATION #############################
explore:fraud_model_evaluation {
  hidden: yes
}
explore: new_transactions_model_info {
  hidden: yes
}
explore: roc_curve {
  hidden: yes
}

# VIEWS:
view: fraud_model_evaluation {
  derived_table: {
    sql: SELECT * FROM ml.EVALUATE(
          MODEL ${fraud_model.SQL_TABLE_NAME},
          (SELECT * FROM ${fraud_model_testing.SQL_TABLE_NAME}));;
  }
  dimension: recall {type: number value_format_name:percent_2}
  dimension: accuracy {type: number value_format_name:percent_2}
  dimension: f1_score {type: number value_format_name:percent_3}
  dimension: log_loss {type: number}
  dimension: roc_auc {type: number}
}

view: roc_curve {
  derived_table: {
    sql: SELECT * FROM ml.ROC_CURVE(
        MODEL ${fraud_model.SQL_TABLE_NAME},
        (SELECT * FROM ${fraud_model_testing.SQL_TABLE_NAME}));;
  }
  dimension: threshold {
    type: number
#     link: {
#       label: "Likely Customers to Purchase"
#       url: "/explore/bqml_ga_demo/ga_sessions?fields=ga_sessions.fullVisitorId,future_purchase_prediction.max_predicted_score&f[future_purchase_prediction.predicted_will_purchase_in_future]=%3E%3D{{value}}"
#       icon_url: "http://www.looker.com/favicon.ico"
#     }
  }
  dimension: recall {type: number value_format_name: percent_2}
  dimension: false_positive_rate {type: number}
  dimension: true_positives {type: number }
  dimension: false_positives {type: number}
  dimension: true_negatives {type: number}
  dimension: false_negatives {type: number }
  dimension: precision {
    type:  number
    value_format_name: percent_2
    sql:  ${true_positives} / NULLIF((${true_positives} + ${false_positives}),0);;
  }
  measure: total_false_positives {
    type: sum
    sql: ${false_positives} ;;
  }
  measure: total_true_positives {
    type: sum
    sql: ${true_positives} ;;
  }
  dimension: threshold_accuracy {
    type: number
    value_format_name: percent_2
    sql:  1.0*(${true_positives} + ${true_negatives}) / NULLIF((${true_positives} + ${true_negatives} + ${false_positives} + ${false_negatives}),0);;
  }
  dimension: threshold_f1 {
    type: number
    value_format_name: percent_3
    sql: 2.0*${recall}*${precision} / NULLIF((${recall}+${precision}),0);;
  }
}

view: new_transactions_model_info {
  derived_table: {
    sql: SELECT  * FROM ml.TRAINING_INFO(MODEL ${fraud_model.SQL_TABLE_NAME});;
  }
  dimension: training_run {type: number}
  dimension: iteration {type: number}
  dimension: loss_raw {sql: ${TABLE}.loss;; type: number hidden:yes}
  dimension: eval_loss {type: number}
  dimension: duration_ms {label:"Duration (ms)" type: number}
  dimension: learning_rate {type: number}
  measure: total_iterations {
    type: count
  }
  measure: loss {
    value_format_name: decimal_2
    type: sum
    sql:  ${loss_raw} ;;
  }
  measure: total_training_time {
    type: sum
    label:"Total Training Time (sec)"
    sql: ${duration_ms}/1000 ;;
    value_format_name: decimal_1
  }
  measure: average_iteration_time {
    type: average
    label:"Average Iteration Time (sec)"
    sql: ${duration_ms}/1000 ;;
    value_format_name: decimal_1
  }
}

########################################## PREDICT NEW TRANSACTIONS ############################

view: current_input {
  derived_table: {
    explore_source: card_transactions {
      column: trans_id {}
      column: amount {}
      column: transaction_distance {}
      column: transaction_hour_of_day {}
      column: difference_from_average_distance {}
      column: percent_diff_from_cat {}
      filters: {
        field: card_transactions.transaction_date
        value: "14 days"
      }
    }
  }
}

view: current_fraud_prediction {
  derived_table: {
    sql_trigger_value: select max(trans_date) from `looker-private-demo.retail_banking.card_transactions`;;
    sql: SELECT * FROM ml.PREDICT(
          MODEL ${fraud_model.SQL_TABLE_NAME},
          (SELECT * FROM ${current_input.SQL_TABLE_NAME}));;
  }

  dimension: predicted_fraud {
    type: yesno
    description: "Is this transaction predicted to be fraud, from ML model"
    group_label: "Fraudulent Indicators"
    #sql: ${TABLE}.predicted_is_fraud;;
    sql: ${predicted_fraud_probability}>.6 ;;
  }

  dimension: predicted_fraud_probability {
    description: "Probability that this transaction is fraud, from ML model"
    group_label: "Fraudulent Indicators"
    type: number
    sql: ${TABLE}.predicted_is_fraud_probs[OFFSET(0)].prob;;
  }

  dimension: trans_id {
    hidden: yes
    primary_key: yes
    action: {
      label: "Refund Transaction"
      url: "https://hooks.zapier.com/hooks/catch/1662138/tvc3zj/"
      form_param: {
        name: "Transaction #"
        type: textarea
        default: "{{ value }}"
      }
    }
  }

  measure: max_predicted_score {
    group_label: "Fraud Predictions"
    type: max
    value_format_name: percent_2
    sql: ${predicted_fraud_probability} ;;
  }

  measure: average_predicted_score {
    group_label: "Fraud Predictions"
    type: average
    value_format_name: percent_2
    sql: ${predicted_fraud_probability} ;;
  }

  measure: number_fraud_predictions {
    group_label: "Fraud Predictions"
    type: count
    filters: [
      predicted_fraud: "yes"
    ]
    drill_fields: [trans_id, card_transactions.amount, card_transactions.merchant, max_predicted_score]
  }
}
