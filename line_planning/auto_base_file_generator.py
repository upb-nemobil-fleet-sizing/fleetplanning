import os
import argparse
import json
from operational_vertices import OperationalVertices
from request_processing_utils import RequestDataframe

def main():
    parser = argparse.ArgumentParser(description="Process request data and generate base data using network constrained clustering.")
    
    # General inputs
    line_select_group = parser.add_mutually_exclusive_group() # for the auto or manual line selection step
    line_select_group.add_argument('--auto_select_lines', dest='auto_select_lines', action='store_true',
                                help='Automatically select the top-N lines. Default: True')
    line_select_group.add_argument('--manual_select_lines', dest='auto_select_lines', action='store_false',
                                help='User will have to manually select the lines instead of auto-selecting top-N')
    parser.set_defaults(auto_select_lines=True)
    
    parser.add_argument("--n_lines", type=int, default=3,
                    help="Number of top lines to keep (only used when --auto_select_lines is enabled). Default: 3")
    parser.add_argument("--fpath_in", type=str, default="data/input",
                        help="Path to instance file or dir containing multiple instance files. Default: data/input")
    parser.add_argument("--fpath_out", type=str, default="BASE_DATA.json",
                        help="Path to output base data JSON file. Default: BASE_DATA_EXAMPLE.json")
    parser.add_argument("--fpath_manual_lines", type=str, default=None,
                        help="Path to JSON file with manualy specified lines. Default: None")
    
    # Bounding box inputs
    parser.add_argument("--sw_lat", type=float, default=51.746200,
                        help="Southwest latitude of the OperationalVertices")
    parser.add_argument("--sw_lon", type=float, default=9.280800,
                        help="Southwest longitude of the OperationalVertices")
    parser.add_argument("--ne_lat", type=float, default=51.797510,
                        help="Northeast latitude of the OperationalVertices")
    parser.add_argument("--ne_lon", type=float, default=9.414761,
                        help="Northeast longitude of the OperationalVertices")

    # Clustering parameters
    parser.add_argument("--eps", type=float, default=200,
                        help="Epsilon parameter for clustering. Default: 200")
    parser.add_argument("--minpts", type=int, default=10,
                        help="Minimum points parameter for clustering. Default: 10")

    args = parser.parse_args()
    
    # check line selection mode
    if args.auto_select_lines:
        if args.n_lines is None:
            raise ValueError("Automatic line selection is enabled, but --n_lines was not provided.")
        print(f"Automatic line selection mode is active. The top {args.n_lines} lines will be used.")
    else:
        print("Manual line selection mode is active.")

    # make sure dir for plots exists
    fname = os.path.splitext(os.path.basename(args.fpath_out))[0] 
    plot_dir = "data/plots/"
    os.makedirs(plot_dir, exist_ok=True)

    print("Setting up RequestDataframe...")
    OV = OperationalVertices([
        (args.sw_lat, args.sw_lon),
        (args.sw_lat, args.ne_lon),
        (args.ne_lat, args.ne_lon),
        (args.ne_lat, args.sw_lon),
    ])
    RDF = RequestDataframe(args.fpath_in, OV)

    print("Automatically determining lines...")
    RDF.determine_lines_with_clustering(eps=args.eps, minpts=args.minpts)

    n_lines_found = len(RDF.lines)
    if n_lines_found == 0:
        print(f"Failed To Determine Any Lines")
        if args.fpath_manual_lines is None:
            # if no lines were determined and no manual lines will be added, end program
            print("Script completed")
            return
        
    print(f"Determined {n_lines_found} lines automatically")

    # add manaual lines
    if args.fpath_manual_lines is not None:
        print(f"Attemptibg to add lines that were manually specified...")
        try:
            with open(args.fpath_manual_lines, 'r') as f:
                json_data = json.load(f)
            
            manual_lines = json_data.get("manual_lines", [])
            
            if not manual_lines:
                print("Manual lines we empty")
            else:
                for line_data in manual_lines:
                    rdf_args = {k: line_data[k] for k in ['start', 'end', 'via'] if k in line_data}
                    RDF.add_line_between_coords(**rdf_args, verbose=True)
                print("Attempting to reassign requests to the manually added lines...")
                RDF.reassign_trips_to_lines()
                    
        except FileNotFoundError:
            print("Error: 'lines.json' file not found. Please make sure the file exists.")
        except json.JSONDecodeError:
            print("Error: Failed to decode JSON. Please check the file format.")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
        
        
        
    plot_fpath = os.path.join(plot_dir, fname + "_INTERMEDIATE_LINES.html")
    RDF.plot_lines_with_chaining_locations(fixed_cl_size=25, save_as=plot_fpath)
    print(f"View intermediate lines at: {plot_fpath}")
        
    line_trip_counts = RDF.df["Line"].value_counts().to_dict()
    line_trip_counts.pop(-1, None)  # remove unassigned ID
    lines_to_keep = None
    
    if args.auto_select_lines: 
        # auto select top n_lines lines
        line_trip_counts = dict(sorted(line_trip_counts.items(), key=lambda item: item[1], reverse=True))
        n_lines = min(args.n_lines, n_lines_found)
        lines_to_keep = list(line_trip_counts.keys())[:n_lines]
        print(f"Reassigning the requests to the {n_lines} most popular lines...")

    else:
        # manually select lines to keep
        while True:
            print(f"Available line IDs: {sorted(line_trip_counts.keys())}")
            user_input = input("Enter the line IDs to keep, separated by commas: ")
            try:
                lines_to_keep = [int(item.strip()) for item in user_input.split(",")] # line ids are ints
            except ValueError:
                print("Invalid input: please enter a comma-separated list of integers.")
                continue

            invalid_ids = [lid for lid in lines_to_keep if lid not in line_trip_counts]
            if invalid_ids:
                print(f"Invalid line IDs: {invalid_ids}")
                print(f"Available line IDs: {sorted(line_trip_counts.keys())}")
                continue

            # Input is valid
            print(f"Reassigning the requests to the selected lines...")
            break
    
    RDF.reassign_trips_to_lines(lines_to_keep)

    print(f"Generating Base Data File...")
    RDF.generate_ini_base_data(fpath=args.fpath_out, line_ids=lines_to_keep)
    print(f"Base Data File saved at: {args.fpath_out}")

    print(f"Saving Plot of Lines...")
    plot_fpath = os.path.join(plot_dir, fname + "_FINAL_LINES.html")
    RDF.plot_lines_with_chaining_locations(line_ids=lines_to_keep, fixed_cl_size=25, save_as=plot_fpath)
    print(f"Plot of Lines saved at: {plot_fpath}")
    print("Script completed")
    
if __name__ == "__main__":
    main()