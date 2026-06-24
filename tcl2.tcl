proc get_collar_name {fastener_name} {
    set fn_lower [string tolower $fastener_name]
    if {[string match "*bg*" $fn_lower]} {
        return "Not applicable"
    } elseif {[string match "*hst*" $fn_lower]} {
        return "STR20K"
    } elseif {[string match "*nas*" $fn_lower]} {
        return "CB6XXX"
    } elseif {[string match "*els*" $fn_lower]} {
        return "EN2182ND"
    } else {
        return "Unknown"
    }
}

proc export_fastener_sets {set_ids} {
    set all_rows {}
    set total_elements 0

    foreach set_id $set_ids {
        set set_name [hm_getentityvalue sets $set_id "name" 1]
        puts "Processing set: $set_name (ID: $set_id)"

        set underscore_idx [string first "_" $set_name]
        if {$underscore_idx < 0} {
            puts "WARNING: Set '$set_name' does not match FASTENERNAME_DIAMETER format. Skipping."
            continue
        }

        set fastener_name     [string range $set_name 0 [expr {$underscore_idx - 1}]]
        set fastener_diameter [string range $set_name [expr {$underscore_idx + 1}] end]
        set collar_name       [get_collar_name $fastener_name]

        puts "  Fastener Name     : $fastener_name"
        puts "  Fastener Diameter : $fastener_diameter"
        puts "  Collar Name       : $collar_name"

        *createmark elems 1 "by set id" $set_id
        set elem_ids [hm_getmark elems 1]

        if {[llength $elem_ids] == 0} {
            puts "WARNING: Set '$set_name' has no elements. Skipping."
            continue
        }

        puts "  Elements found: [llength $elem_ids]"

        foreach eid $elem_ids {
            lappend all_rows "$eid,$fastener_name,$fastener_diameter,$collar_name"
        }

        incr total_elements [llength $elem_ids]
    }

    if {$total_elements == 0} {
        puts "ERROR: No elements found across all selected sets."
        return
    }

    # Ask for save directory once, after processing all sets
    set save_dir [tk_chooseDirectory -title "Select folder to save CSV"]
    if {$save_dir eq ""} {
        puts "ERROR: No folder selected."
        return
    }

    set timestamp [clock seconds]
    set time_str  [clock format $timestamp -format "%H%M%S"]
    set date_str  [clock format $timestamp -format "%Y%m%d"]
    set filename  [file join $save_dir "JOINT-${time_str}-${date_str}.csv"]

    set fh [open $filename w]
    puts $fh "Element ID,Fastener Name,Fastener Diameter,Collar Name"

    foreach row $all_rows {
        puts $fh $row
    }

    close $fh
    puts "---"
    puts "Total elements exported : $total_elements"
    puts "Output file             : $filename"
}

# --- Let user pick MULTIPLE sets from HyperMesh panel ---
*createmarkpanel sets 1 "Select one or more fastener sets and click proceed"
set selected_ids [hm_getmark sets 1]

if {[llength $selected_ids] == 0} {
    puts "ERROR: No sets selected."
} else {
    puts "Sets selected: [llength $selected_ids]"
    export_fastener_sets $selected_ids
}
