# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "11111111-1111-1111-1111-111111111111",
# META       "default_lakehouse_name": "lh_sales",
# META       "default_lakehouse_workspace_id": "22222222-2222-2222-2222-222222222222"
# META     }
# META   }
# META }

# CELL ********************

# Sample notebook. The lakehouse and workspace ids above are rewritten per
# environment by config/parameter.yml when the pipeline deploys this item.
df = spark.createDataFrame([(1, "contoso"), (2, "fabrikam")], ["id", "customer"])
df.write.mode("overwrite").format("delta").saveAsTable("dim_customer")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
