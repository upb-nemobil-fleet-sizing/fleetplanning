import pandas as pd
import argparse
import os
import json
from pathlib import Path
import dash
from dash import dash_table, html, dcc
import plotly.express as px

parser = argparse.ArgumentParser()
parser.add_argument(
    "dirs", 
    nargs="+",                 # one or more arguments required
    help="One or more directories"
) 
parser.add_argument("--instance", help="Limit result display to one instance", default=None)
args = parser.parse_args()

if args.instance: 
	rejects = []
	num_platoons = []
	configs = []
	for direct in args.dirs:
		configs.append(direct)
		kpi_file = Path(__file__).resolve().parent / direct / "general_kpi" / f"kpi_{args.instance}.json"
		with open(kpi_file, "r") as f:
			data = json.load(f)
			rejects.append(data["num_rejects"])
		platoon_file  = Path(__file__).resolve().parent / direct / "platoon_results" / f"platoon_info_{args.instance}.json"
		with open(platoon_file, "r") as f:
			data = json.load(f)
			num_platoons.append(data["total_num_platoons"])



	x = np.arange(len(configs))
	width = 0.35

	fig, ax1 = plt.subplots()

	# First axis (left y-axis)
	bars1 = ax1.bar(x - width/2, rejects, width, label="Rejects", color="skyblue")
	ax1.set_ylabel("Rejects")
	ax1.set_xticks(x)
	ax1.set_xticklabels(configs)

	# Second axis (right y-axis)
	ax2 = ax1.twinx()
	bars2 = ax2.bar(x + width/2, num_platoons, width, label="Num Platoons", color="salmon")
	ax2.set_ylabel("Num Platoons")

	# Title
	plt.title(f"Comparison of Rejects and Number of Platoons Across Configurations for {args.instance}")

	# Proper legend handling
	handles1, labels1 = ax1.get_legend_handles_labels()
	handles2, labels2 = ax2.get_legend_handles_labels()
	ax1.legend(handles1 + handles2, labels1 + labels2, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=2)

	plt.show()

else:
	dfs = {}
	for direct in args.dirs:
		dfs[direct] = []
		df_config = pd.DataFrame(columns = ["instance"])
		kpi_direct = Path(__file__).resolve().parent.parent / direct / "general_kpi" 
		platoon_direct = Path(__file__).resolve().parent.parent / direct / "platoon_results" 
		for index, kpi_file in enumerate(kpi_direct.iterdir()):
			instance = "_".join(kpi_file.name.split(".")[0].split("_")[1:])
			row = [instance]
			with open(kpi_file, "r") as f:
				data = json.load(f)
				for key in data.keys():

					if index == 0:
						df_config[key] = None
					row.append(data[key])
			df_config.loc[len(df_config)] = row

		dfs[direct].append(df_config)
		df_config = pd.DataFrame(columns = ["instance"])
		for index, platoon_file in enumerate(platoon_direct.iterdir()):
			instance = "_".join(platoon_file.name.split(".")[0].split("_")[2:])
			row = [instance]
			with open(platoon_file, "r") as f:
				data = json.load(f)
				for key in data.keys():
					if key != "platoon_reservations" and key != "deleted_cs":
						if index == 0:
							df_config[key] = None
						row.append(data[key])
			df_config.loc[len(df_config)] = row

		dfs[direct].append(df_config)


	app = dash.Dash(__name__)

	def make_table(df):
	    # Compute max/min for numeric columns
	    style_cond = []
	    for c in df.select_dtypes(include='number').columns:
	        col_max = df[c].max()
	        col_min = df[c].min()
	        style_cond += [
	            {
	                "if": {"column_id": c, "filter_query": f"{{{c}}} = {col_max}"},
	                "backgroundColor": "lightgreen",
	                "fontWeight": "bold"
	            },
	            {
	                "if": {"column_id": c, "filter_query": f"{{{c}}} = {col_min}"},
	                "backgroundColor": "salmon",
	                "fontWeight": "bold"
	            }
	        ]
	    
	    return dash_table.DataTable(
	        data=df.to_dict("records"),
	        columns=[{"name": c, "id": c} for c in df.columns],
	        style_table={"overflowX": "auto", "minWidth": "300px"},
	        style_cell={"textAlign": "center", "padding": "5px", "minWidth": "80px"},
	        style_header={"backgroundColor": "#f8f9fa", "fontWeight": "bold"},
	        sort_action="native",
	        filter_action="native",
	        page_action="none",
	    )


	sections = []
	for key, (dfa, dfb) in dfs.items():
	    sections.append(html.H2(key, style={"marginTop": "30px"}))
	    sections.append(html.Div([
	        html.Div([html.H4("General KPI"), make_table(dfa)], style={"flex": "1", "padding": "10px"}),
	        html.Div([html.H4("Platoon KPI"), make_table(dfb)], style={"flex": "1", "padding": "10px"})
	    ], style={"display": "flex", "gap": "20px"}))

	app.layout = html.Div(sections, style={"padding": "40px", "fontFamily": "Arial"})

	if __name__ == "__main__":
	    app.run(debug=True)




