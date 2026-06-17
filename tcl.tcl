proc export_fastener_set {set_id set_name} {

    set underscore_idx [string first "_" $set_name]
    if {$underscore_idx < 0} {
        puts "ERROR: Set '$set_name' does not match FASTENERNAME_DIAMETER format."
        return
    }

    set fastener_name     [string range $set_name 0 [expr {$underscore_idx - 1}]]
    set fastener_diameter [string range $set_name [expr {$underscore_idx + 1}] end]

    puts "Fastener Name     : $fastener_name"
    puts "Fastener Diameter : $fastener_diameter"

    # Get elements from set using hm_getentityvalue
    *createmark elems 1 "by set id" $set_id
    set elem_ids [hm_getmark elems 1]
    puts "by set id: $elem_ids"

    if {[llength $elem_ids] == 0} {
        puts "WARNING: Set '$set_name' has no elements."
        return
    }

    puts "Elements found: [llength $elem_ids]"

    set timestamp [clock seconds]
    set time_str  [clock format $timestamp -format "%H%M%S"]
    set date_str  [clock format $timestamp -format "%Y%m%d"]
    set save_dir [tk_chooseDirectory -title "Select folder to save CSV"]
    if {$save_dir eq ""} {
        puts "ERROR: No folder selected."
        return
    }
    set filename [file join $save_dir "JOINT-${time_str}-${date_str}.csv"]

    set fh [open $filename w]
    puts $fh "Element ID,Fastener Name,Fastener Diameter"
    foreach eid $elem_ids {
        puts $fh "$eid,$fastener_name,$fastener_diameter"
    }
    close $fh

    puts "Exported [llength $elem_ids] elements to: $filename"
}

# --- Let user pick a set from HyperMesh panel ---
*createmarkpanel sets 1 "Select a fastener set and click proceed"

set set_id [lindex [hm_getmark sets 1] 0]

if {$set_id eq "" || $set_id == 0} {
    puts "ERROR: No set selected."
} else {
    set set_name [hm_getentityvalue sets $set_id "name" 1]
    puts "Selected set: $set_name"
    export_fastener_set $set_id $set_name
}

