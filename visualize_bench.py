#!/usr/bin/env python3

import csv
import sys
import statistics
from pathlib import Path
from collections import defaultdict
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

SCHNORR_BASELINE = 80000
AVERAGE_CUTOFF = SCHNORR_BASELINE / 2
CUTOFF_YEAR = 2016

CPU_YEARS = {
    "apple m1 pro": 2021, "apple m2": 2022, "apple m4 pro": 2024,
    "rpi 5": 2023, "rpi5": 2023, "rpi5-8": 2023, "cortex-a76": 2023,
    "amd ryzen 5 3600": 2019, "amd ryzen 7 5800u": 2021,
    "amd ryzen 9 9950x": 2024, "intel xeon e5-2637": 2012,
    "i5-12500": 2022, "i7-7700": 2017, "i7-8700": 2018,
    "i9-9900k": 2018, "n150": 2024,
}


def get_cpu_year(cpu_name: str) -> int | None:
    cpu_lower = cpu_name.lower()
    for key, year in CPU_YEARS.items():
        if key in cpu_lower:
            return year
    return None


def extract_opcode(name: str) -> str:
    """Extract the core opcode from a benchmark name like '3DUP_HASH256_DROP_...' -> 'HASH256'"""
    # Known opcodes (including ones with numbers)
    known_opcodes = {'HASH256', 'SHA256', 'HASH160', 'SHA1', 'RIPEMD160', 'LSHIFT', 'RSHIFT',
                     'AND', 'OR', 'XOR', 'NOT', 'CAT', 'SUBSTR', 'MUL', 'DIV', 'MOD'}
    parts = name.split('_')
    for part in parts:
        if part in known_opcodes:
            return part
        if part.endswith('DUP') or part == 'DUP':
            continue
        if part == 'DROP':
            continue
        if any(c.isdigit() for c in part):
            continue
        if part.lower() == 'shift':
            continue
        return part
    return name


def extract_machine_info(filepath: str) -> dict:
    info = {'cpu': 'Unknown', 'arch': 'Unknown', 'file': Path(filepath).name}
    with open(filepath, 'r') as f:
        for line in f:
            if not line.startswith('#'):
                break
            if 'CPU:' in line:
                info['cpu'] = line.split('CPU:')[1].strip()
            elif 'Architecture:' in line:
                info['arch'] = line.split('Architecture:')[1].strip()
    return info


def parse_csv(filepath: str) -> tuple[list[dict], dict]:
    results = []
    machine_info = extract_machine_info(filepath)
    
    with open(filepath, 'r') as f:
        lines = [line for line in f if not line.startswith('#')]
    
    reader = csv.DictReader(lines)
    for row in reader:
        varops_pct = float(row['Varops_Percentage'])
        if row['Name'] == 'Schnorr signature validation':
            varops_pct = 100.0
        results.append({
            'rank': int(row['Rank']),
            'name': row['Name'],
            'seconds': float(row['Seconds']),
            'schnorr_equivalents': float(row['Schnorr_Equivalents']),
            'varops_percentage': varops_pct,
            'is_gsr_only': row['Is_GSR_Only'].lower() == 'true'
        })
    return results, machine_info


def parse_multiple_csvs(filepaths: list[str]) -> tuple[list[dict], list[dict]]:
    all_machine_data = []
    benchmark_data = defaultdict(lambda: {'seconds': [], 'schnorr_equivalents': [], 'varops_percentage': [], 'is_gsr_only': None, 'schnorr_equivalents_max': 0})
    
    for filepath in filepaths:
        results, machine_info = parse_csv(filepath)
        all_machine_data.append((results, machine_info))
        
        for r in results:
            name = r['name']
            benchmark_data[name]['seconds'].append(r['seconds'])
            benchmark_data[name]['schnorr_equivalents'].append(r['schnorr_equivalents'])
            benchmark_data[name]['varops_percentage'].append(r['varops_percentage'])
            benchmark_data[name]['schnorr_equivalents_max'] = max(benchmark_data[name]['schnorr_equivalents_max'], r['schnorr_equivalents'])
            if benchmark_data[name]['is_gsr_only'] is None:
                benchmark_data[name]['is_gsr_only'] = r['is_gsr_only']
    
    averaged_results = []
    for name, data in benchmark_data.items():
        n = len(data['seconds'])
        avg_seconds = sum(data['seconds']) / n
        std_seconds = statistics.stdev(data['seconds']) if n > 1 else 0

        averaged_results.append({
            'name': name,
            'seconds': avg_seconds,
            'seconds_std': std_seconds,
            'schnorr_equivalents': sum(data['schnorr_equivalents']) / n,
            'varops_percentage': sum(data['varops_percentage']) / n,
            'is_gsr_only': data['is_gsr_only'],
            'seconds_all': data['seconds'],
            'schnorr_equivalents_all': data['schnorr_equivalents'],
            'schnorr_equivalents_max': data['schnorr_equivalents_max'],
        })
    
    return averaged_results, all_machine_data


def analyze_results(results: list[dict]) -> tuple[list[dict], list[dict]]:
    current_script = []
    gsr_added = []
    
    for r in results:
        if r['is_gsr_only']:
            gsr_added.append(r)
        else:
            current_script.append(r)
    
    current_script.sort(key=lambda x: x['seconds'], reverse=True)
    gsr_added.sort(key=lambda x: x['seconds'], reverse=True)
    
    return current_script, gsr_added


def print_summary(current_script: list[dict], gsr_added: list[dict], num_machines: int):
    print("\nBENCHMARK ANALYSIS")
    print(f"Averaged across {num_machines} machine(s)\n")
    
    print("\nPRE-GSR OPERATIONS (Current Bitcoin Script + Schnorr)")
    if current_script:
        print(f"{'Rank':<6} {'Operation':<45} {'Time (s)':<12} {'Varops %'}")
        for i, r in enumerate(current_script[:10], 1):
            print(f"{i:<6} {r['name']:<45} {r['seconds']:<12.3f} {r['varops_percentage']:.1f}%")
        
        worst = current_script[0]
        print(f"\nWorst case: {worst['name']}")
        std = worst.get('seconds_std', 0)
        if std > 0:
            print(f"Time: {worst['seconds']:.3f} ± {std:.3f}s")
        else:
            print(f"Time: {worst['seconds']:.3f}s")
    else:
        print("No pre-GSR operations found")
    
    print("\nNEW GSR OPERATIONS")
    if gsr_added:
        print(f"{'Rank':<6} {'Operation':<45} {'Time (s)':<12} {'Varops %'}")
        for i, r in enumerate(gsr_added[:10], 1):
            print(f"{i:<6} {r['name']:<45} {r['seconds']:<12.3f} {r['varops_percentage']:.1f}%")
        
        worst = gsr_added[0]
        print(f"\nWorst case: {worst['name']}")
        std = worst.get('seconds_std', 0)
        if std > 0:
            print(f"Time: {worst['seconds']:.3f} ± {std:.3f}s")
        else:
            print(f"Time: {worst['seconds']:.3f}s")
    else:
        print("No new GSR operations found")
    
    print("\nCOMPARISON")
    if current_script and gsr_added:
        curr_worst = current_script[0]['seconds']
        gsr_worst = gsr_added[0]['seconds']
        print(f"Worst pre-GSR:  {curr_worst:.3f}s ({current_script[0]['name']})")
        print(f"Worst GSR:      {gsr_worst:.3f}s ({gsr_added[0]['name']})")
        ratio = gsr_worst / curr_worst
        if ratio <= 1.0:
            print(f"GSR worst is {ratio:.2f}x the pre-GSR worst (GSR is not slower)")
        else:
            print(f"GSR worst is {ratio:.2f}x the pre-GSR worst")
    print()


def create_averaged_visualization(current_script: list[dict], gsr_added: list[dict],
                                   num_machines: int, output_path: str):
    curr_top = sorted(current_script, key=lambda r: r['seconds'], reverse=True)[:5]
    gsr_top = sorted(gsr_added, key=lambda r: r['seconds'], reverse=True)[:5]

    curr_color = '#3498db'
    gsr_color = '#27ae60'
    ref_color = '#e74c3c'

    def shorten(name: str) -> str:
        return name[:35] + '...' if len(name) > 35 else name

    curr_labels = [f"[{r['varops_percentage']:.0f}%] {shorten(r['name'])}" for r in curr_top]
    gsr_labels = [f"[{r['varops_percentage']:.0f}%] {shorten(r['name'])}" for r in gsr_top]

    gap = 1
    curr_x = list(range(len(curr_top)))
    gsr_x = [x + len(curr_top) + gap for x in range(len(gsr_top))]

    fig, ax = plt.subplots(figsize=(14, 7))
    subtitle = f'Top 5 worst cases each — mean ± std across {num_machines} machine(s)'
    fig.suptitle(f'Worst Case Block Sized Script: Pre-GSR vs New GSR Operations\n({subtitle})',
                 fontsize=13, fontweight='bold')

    for i, (r, x) in enumerate(zip(curr_top, curr_x)):
        std = r.get('seconds_std', 0)
        ax.errorbar(x, r['seconds'], yerr=std, fmt='o', color=curr_color,
                    markersize=8, capsize=6, capthick=2, elinewidth=2, zorder=5)

    for i, (r, x) in enumerate(zip(gsr_top, gsr_x)):
        std = r.get('seconds_std', 0)
        ax.errorbar(x, r['seconds'], yerr=std, fmt='o', color=gsr_color,
                    markersize=8, capsize=6, capthick=2, elinewidth=2, zorder=5)

    # Pre-GSR worst case reference line across GSR section
    pre_gsr_worst = curr_top[0]['seconds'] if curr_top else 0
    if pre_gsr_worst > 0 and gsr_x:
        ax.axhline(y=pre_gsr_worst, color=ref_color, linestyle='--', linewidth=1.5,
                   label=f'Pre-GSR worst ({pre_gsr_worst:.2f}s)', zorder=3)

    if curr_x and gsr_x:
        divider_x = (curr_x[-1] + gsr_x[0]) / 2
        ax.axvline(x=divider_x, color='#aaaaaa', linestyle=':', linewidth=1.5)

    if curr_x:
        ax.text((curr_x[0] + curr_x[-1]) / 2, 1.02,
                'Pre-GSR (incl. Schnorr 80k sigs)', ha='center', va='bottom', fontsize=11,
                fontweight='bold', color=curr_color, transform=ax.get_xaxis_transform())
    if gsr_x:
        ax.text((gsr_x[0] + gsr_x[-1]) / 2, 1.02,
                'New Operations Added by GSR', ha='center', va='bottom', fontsize=11,
                fontweight='bold', color=gsr_color, transform=ax.get_xaxis_transform())

    ax.set_xticks(curr_x + gsr_x)
    ax.set_xticklabels(curr_labels + gsr_labels, rotation=30, ha='right', fontsize=8)

    if curr_x:
        ax.axvspan(curr_x[0] - 0.5, curr_x[-1] + 0.5, alpha=0.04, color=curr_color)
    if gsr_x:
        ax.axvspan(gsr_x[0] - 0.5, gsr_x[-1] + 0.5, alpha=0.04, color=gsr_color)

    ax.set_ylabel('Execution Time (seconds)', fontsize=11)
    ax.set_xlabel('Operation', fontsize=11)
    ax.yaxis.grid(True, alpha=0.3)
    ax.set_axisbelow(True)

    from matplotlib.lines import Line2D
    legend_handles = [
        Line2D([0], [0], marker='o', color='w', markerfacecolor=curr_color, markersize=8, label='Pre-GSR (mean ± std)'),
        Line2D([0], [0], marker='o', color='w', markerfacecolor=gsr_color, markersize=8, label='New GSR Op (mean ± std)'),
        Line2D([0], [0], color=ref_color, linestyle='--', linewidth=1.5, label=f'Pre-GSR worst ({pre_gsr_worst:.2f}s)'),
    ]
    ax.legend(handles=legend_handles, fontsize=9, loc='upper right')

    plt.tight_layout(rect=[0, 0, 1, 0.93])
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved: {output_path}")


def create_schnorr_equivalents_visualization(current_script: list[dict], gsr_added: list[dict],
                                             num_machines: int, output_path: str):
    curr_top = sorted(current_script, key=lambda r: r['schnorr_equivalents'], reverse=True)[:10]
    gsr_top = sorted(gsr_added, key=lambda r: r['schnorr_equivalents'], reverse=True)[:10]

    max_operations = max(len(curr_top), len(gsr_top))
    fig_height = max(10, min(30, max_operations * 0.6))
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, fig_height))
    subtitle = f'Top 10 worst cases by average Schnorr equivalents across {num_machines} machine(s)'
    fig.suptitle(f'Worst Case Block Sized Script: Pre-GSR vs New GSR Operations\n({subtitle})',
                 fontsize=14, fontweight='bold')

    curr_color = '#3498db'
    gsr_color = '#27ae60'
    schnorr_color = '#e74c3c'

    if curr_top:
        names = [r['name'][:40] + '...' if len(r['name']) > 40 else r['name'] for r in curr_top]

        for i, r in enumerate(curr_top):
            schnorr_eqs_all = r.get('schnorr_equivalents_all', [r['schnorr_equivalents']])
            y_pos = [i] * len(schnorr_eqs_all)

            ax1.scatter(schnorr_eqs_all, y_pos, color=curr_color, alpha=0.6, s=50, edgecolors='white', linewidth=0.5)
            ax1.scatter(r['schnorr_equivalents'], i, color=curr_color, s=100, marker='D', edgecolors='black', linewidth=1, zorder=5)

        ax1.axvline(x=SCHNORR_BASELINE, color=schnorr_color, linestyle='--', linewidth=2, label=f'Block limit ({SCHNORR_BASELINE:,} sigs)')
        # Add legend entries for markers
        ax1.scatter([], [], color=curr_color, alpha=0.6, s=50, edgecolors='white', linewidth=0.5, label='Individual machines')
        ax1.scatter([], [], color=curr_color, s=100, marker='D', edgecolors='black', linewidth=1, label='Mean across machines')
        ax1.set_yticks(range(len(names)))
        ax1.set_yticklabels(names, fontsize=8)
        ax1.set_xlabel('Schnorr Signature Equivalents (per block)')
        ax1.set_title('Pre-GSR (incl. Schnorr 80k sigs)', fontsize=12, fontweight='bold', color=curr_color)
        ax1.invert_yaxis()
        ax1.legend(loc='lower right', fontsize=8)
        ax1.grid(axis='x', alpha=0.3)
        ax1.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: format(int(x), ',')))

    else:
        ax1.text(0.5, 0.5, 'No pre-GSR operations', ha='center', va='center', transform=ax1.transAxes)
        ax1.set_title('Pre-GSR (incl. Schnorr 80k sigs)', fontsize=12, fontweight='bold', color=curr_color)

    if gsr_top:
        names = [r['name'][:40] + '...' if len(r['name']) > 40 else r['name'] for r in gsr_top]

        for i, r in enumerate(gsr_top):
            schnorr_eqs_all = r.get('schnorr_equivalents_all', [r['schnorr_equivalents']])
            y_pos = [i] * len(schnorr_eqs_all)

            ax2.scatter(schnorr_eqs_all, y_pos, color=gsr_color, alpha=0.6, s=50, edgecolors='white', linewidth=0.5)
            ax2.scatter(r['schnorr_equivalents'], i, color=gsr_color, s=100, marker='D', edgecolors='black', linewidth=1, zorder=5)

        ax2.axvline(x=SCHNORR_BASELINE, color=schnorr_color, linestyle='--', linewidth=2, label=f'Block limit ({SCHNORR_BASELINE:,} sigs)')
        # Add legend entries for markers
        ax2.scatter([], [], color=gsr_color, alpha=0.6, s=50, edgecolors='white', linewidth=0.5, label='Individual machines')
        ax2.scatter([], [], color=gsr_color, s=100, marker='D', edgecolors='black', linewidth=1, label='Mean across machines')
        ax2.set_yticks(range(len(names)))
        ax2.set_yticklabels(names, fontsize=8)
        ax2.set_xlabel('Schnorr Signature Equivalents (per block)')
        ax2.set_title('New Operations Added by GSR', fontsize=12, fontweight='bold', color=gsr_color)
        ax2.invert_yaxis()
        ax2.legend(loc='lower right', fontsize=8)
        ax2.grid(axis='x', alpha=0.3)
        ax2.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: format(int(x), ',')))

    else:
        ax2.text(0.5, 0.5, 'No new GSR operations', ha='center', va='center', transform=ax2.transAxes)
        ax2.set_title('New Operations Added by GSR', fontsize=12, fontweight='bold', color=gsr_color)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved: {output_path}")


def get_machine_sort_key(machine_info: dict) -> tuple:
    """
    Returns a sort key tuple: (arch_order, vendor_order, cpu_name)
    Groups: ARM first, then x86
    Subgroups: Apple, AMD, Intel, Other
    """
    cpu = machine_info['cpu'].lower()
    arch = machine_info['arch'].lower()
    
    if 'arm' in arch or 'aarch' in arch:
        arch_order = 0
    else:
        arch_order = 1
    
    if 'apple' in cpu or 'm1' in cpu or 'm2' in cpu or 'm3' in cpu or 'm4' in cpu:
        vendor_order = 0
    elif 'amd' in cpu or 'ryzen' in cpu:
        vendor_order = 1
    elif 'intel' in cpu or 'core' in cpu:
        vendor_order = 2
    else:
        vendor_order = 3
    
    return (arch_order, vendor_order, machine_info['cpu'])


def create_per_machine_visualization(all_machine_data: list[tuple], output_path: str):
    num_machines = len(all_machine_data)
    if num_machines == 0:
        return
    
    all_machine_data = sorted(all_machine_data, key=lambda x: get_machine_sort_key(x[1]))
    
    machine_names = []
    curr_worst_times = []
    curr_worst_names = []
    gsr_worst_times = []
    gsr_worst_names = []
    gsr_all_ops = []
    
    for results, machine_info in all_machine_data:
        cpu_short = machine_info['cpu'][:30] + '...' if len(machine_info['cpu']) > 30 else machine_info['cpu']
        machine_name = f"{cpu_short}\n({machine_info['arch']})"
        machine_names.append(machine_name)
        
        current_script, gsr_added = analyze_results(results)
        gsr_all_ops.append(gsr_added)
        
        if current_script:
            curr_worst_times.append(current_script[0]['seconds'])
            curr_worst_names.append(current_script[0]['name'][:25] + '...' if len(current_script[0]['name']) > 25 else current_script[0]['name'])
        else:
            curr_worst_times.append(0)
            curr_worst_names.append('N/A')
        
        if gsr_added:
            gsr_worst_times.append(gsr_added[0]['seconds'])
            gsr_worst_names.append(gsr_added[0]['name'][:25] + '...' if len(gsr_added[0]['name']) > 25 else gsr_added[0]['name'])
        else:
            gsr_worst_times.append(0)
            gsr_worst_names.append('N/A')
    
    fig, ax = plt.subplots(figsize=(14, max(6, num_machines * 1.5)))
    fig.suptitle('Worst Case Block Sized Script: Performance Across All Machines', 
                 fontsize=14, fontweight='bold')
    
    y_pos = range(num_machines)
    bar_height = 0.3
    
    curr_color = '#3498db'
    gsr_color = '#27ae60'
    
    bars1 = ax.barh([y - bar_height/2 for y in y_pos], curr_worst_times, bar_height, 
                    label='Pre-GSR Worst (incl. Schnorr)', color=curr_color, alpha=0.8)
    bars2 = ax.barh([y + bar_height/2 for y in y_pos], gsr_worst_times, bar_height,
                    label='New GSR Worst', color=gsr_color, alpha=0.8)
    
    for i, (bar1, bar2) in enumerate(zip(bars1, bars2)):
        if curr_worst_times[i] > 0:
            ax.text(bar1.get_width() + 0.02, bar1.get_y() + bar1.get_height()/2,
                   f'{curr_worst_times[i]:.2f}s', va='center', fontsize=8)
        if gsr_worst_times[i] > 0:
            gsr_label = f'{gsr_worst_times[i]:.2f}s'
            if gsr_worst_times[i] > curr_worst_times[i] and curr_worst_times[i] > 0:
                factor = gsr_worst_times[i] / curr_worst_times[i]
                slower_opcodes = set(extract_opcode(op['name']) for op in gsr_all_ops[i] if op['seconds'] > curr_worst_times[i])
                slower_ops_str = ', '.join(sorted(slower_opcodes))
                gsr_label += f' ({factor:.1f}x slower: {slower_ops_str})'
            ax.text(bar2.get_width() + 0.02, bar2.get_y() + bar2.get_height()/2,
                   gsr_label, va='center', fontsize=8)
    
    ax.set_yticks(y_pos)
    ax.set_yticklabels(machine_names, fontsize=9)
    ax.set_xlabel('Execution Time (seconds)')
    ax.set_ylabel('Machine')
    ax.invert_yaxis()
    ax.grid(axis='x', alpha=0.3)
    
    legend_elements = [
        mpatches.Patch(facecolor=curr_color, alpha=0.8, label='Pre-GSR Worst Case (incl. Schnorr 80k sigs)'),
        mpatches.Patch(facecolor=gsr_color, alpha=0.8, label='New GSR Worst Case'),
    ]
    ax.legend(handles=legend_elements, loc='lower right')
    
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved: {output_path}")
    
    print("\nPER-MACHINE WORST CASES")
    print(f"{'Machine':<40} {'Pre-GSR':<15} {'New GSR':<15}")
    for i, (results, machine_info) in enumerate(all_machine_data):
        cpu_short = machine_info['cpu'][:35] + '...' if len(machine_info['cpu']) > 35 else machine_info['cpu']
        print(f"{cpu_short:<40} {curr_worst_times[i]:<15.3f} {gsr_worst_times[i]:<15.3f}")
    print()


def get_vendor_name(cpu: str) -> str:
    """Determine vendor from CPU name."""
    cpu_lower = cpu.lower()
    if 'apple' in cpu_lower or any(f'm{i}' in cpu_lower for i in range(1, 10)):
        return 'Apple'
    elif 'amd' in cpu_lower or 'ryzen' in cpu_lower:
        return 'AMD'
    elif 'intel' in cpu_lower or 'core' in cpu_lower:
        return 'Intel'
    elif 'rpi' in cpu_lower or 'raspberry' in cpu_lower or 'bcm' in cpu_lower or 'arm' in cpu_lower:
        return 'ARM (RPi/Other)'
    else:
        return 'Other'


def create_machine_scatter_plot(all_machine_data: list[tuple], output_path: str):
    """Create a scatter plot comparing current vs GSR worst case performance per machine."""
    if not all_machine_data:
        return
    
    machine_data = []
    for results, machine_info in all_machine_data:
        current_script, gsr_added = analyze_results(results)
        
        curr_worst = current_script[0]['seconds'] if current_script else 0
        gsr_worst = gsr_added[0]['seconds'] if gsr_added else 0
        
        vendor = get_vendor_name(machine_info['cpu'])
        cpu_short = machine_info['cpu'][:25] + '...' if len(machine_info['cpu']) > 25 else machine_info['cpu']
        
        machine_data.append({
            'cpu': cpu_short,
            'vendor': vendor,
            'arch': machine_info['arch'],
            'curr_worst': curr_worst,
            'gsr_worst': gsr_worst,
            'gsr_name': gsr_added[0]['name'] if gsr_added else '',
            'curr_name': current_script[0]['name'] if current_script else '',
        })
    
    fig, ax = plt.subplots(figsize=(12, 10))
    fig.suptitle('Worst Case Block Sized Script: Machine Performance Comparison',
                 fontsize=14, fontweight='bold')
    
    # Vendor colors
    vendor_colors = {
        'Apple': '#666666',
        'AMD': '#ED1C24',
        'Intel': '#0071C5',
        'ARM (RPi/Other)': '#76B900',
        'Other': '#9B59B6'
    }
    
    # Scatter points per vendor
    for vendor in vendor_colors:
        vendor_machines = [m for m in machine_data if m['vendor'] == vendor]
        if not vendor_machines:
            continue
        
        x = [m['curr_worst'] for m in vendor_machines]
        y = [m['gsr_worst'] for m in vendor_machines]
        
        ax.scatter(x, y, c=vendor_colors[vendor], label=vendor, s=150, alpha=0.8, 
                   edgecolors='black', linewidth=1, zorder=5)
    
    # Add machine labels
    for m in machine_data:
        ax.annotate(m['cpu'], (m['curr_worst'], m['gsr_worst']),
                   xytext=(5, 5), textcoords='offset points', fontsize=7, alpha=0.8)
    
    # Diagonal parity line (GSR = Current)
    max_val = max(max(m['curr_worst'] for m in machine_data), 
                  max(m['gsr_worst'] for m in machine_data)) * 1.1
    ax.plot([0, max_val], [0, max_val], 'k--', alpha=0.5, linewidth=1.5, 
            label='Parity (GSR = Current)')
    
    # Add regions annotation
    ax.fill_between([0, max_val], [0, max_val], [max_val, max_val], 
                    alpha=0.1, color='red', label='GSR slower')
    ax.fill_between([0, max_val], [0, 0], [0, max_val], 
                    alpha=0.1, color='green', label='GSR faster')
    
    ax.set_xlabel('Pre-GSR Worst Case incl. Schnorr (seconds)', fontsize=11)
    ax.set_ylabel('New GSR Worst Case (seconds)', fontsize=11)
    ax.set_xlim(0, max_val)
    ax.set_ylim(0, max_val)
    ax.set_aspect('equal')
    ax.grid(True, alpha=0.3)
    ax.legend(loc='upper left', fontsize=9)
    
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved: {output_path}")


def create_vendor_grouped_visualization(all_machine_data: list[tuple], output_path: str):
    """Create a visualization grouping machines by vendor (Apple, AMD, Intel)."""
    if not all_machine_data:
        return
    
    # Group machines by vendor
    vendor_data = defaultdict(lambda: {
        'curr_worst_times': [], 'gsr_worst_times': [],
        'gsr_all_ops': [], 'machines': []
    })
    
    for results, machine_info in all_machine_data:
        vendor = get_vendor_name(machine_info['cpu'])
        current_script, gsr_added = analyze_results(results)
        
        vendor_data[vendor]['machines'].append(machine_info['cpu'])
        vendor_data[vendor]['gsr_all_ops'].extend(gsr_added)
        
        if current_script:
            vendor_data[vendor]['curr_worst_times'].append(current_script[0]['seconds'])
        if gsr_added:
            vendor_data[vendor]['gsr_worst_times'].append(gsr_added[0]['seconds'])
    
    vendor_names = []
    avg_curr_worst = []
    avg_gsr_worst = []
    vendor_gsr_ops = []
    machine_counts = []
    
    vendor_order = ['Apple', 'AMD', 'Intel', 'ARM (RPi/Other)', 'Other']
    for vendor in vendor_order:
        if vendor not in vendor_data:
            continue
        data = vendor_data[vendor]
        n = len(data['machines'])
        vendor_names.append(f"{vendor}\n({n} machine{'s' if n > 1 else ''})")
        machine_counts.append(n)
        
        avg_curr_worst.append(sum(data['curr_worst_times']) / len(data['curr_worst_times']) if data['curr_worst_times'] else 0)
        avg_gsr_worst.append(sum(data['gsr_worst_times']) / len(data['gsr_worst_times']) if data['gsr_worst_times'] else 0)
        vendor_gsr_ops.append(data['gsr_all_ops'])
    
    num_vendors = len(vendor_names)
    fig, ax = plt.subplots(figsize=(12, max(5, num_vendors * 1.8)))
    fig.suptitle('Worst Case Block Sized Script: Performance by Hardware Vendor (Averaged)', 
                 fontsize=14, fontweight='bold')
    
    y_pos = range(num_vendors)
    bar_height = 0.3
    
    curr_color = '#3498db'
    gsr_color = '#27ae60'
    
    bars1 = ax.barh([y - bar_height/2 for y in y_pos], avg_curr_worst, bar_height, 
                    label='Pre-GSR Worst (avg)', color=curr_color, alpha=0.8)
    bars2 = ax.barh([y + bar_height/2 for y in y_pos], avg_gsr_worst, bar_height,
                    label='New GSR Worst (avg)', color=gsr_color, alpha=0.8)
    
    for i, (bar1, bar2) in enumerate(zip(bars1, bars2)):
        if avg_curr_worst[i] > 0:
            ax.text(bar1.get_width() + 0.05, bar1.get_y() + bar1.get_height()/2,
                   f'{avg_curr_worst[i]:.2f}s', va='center', fontsize=9)
        if avg_gsr_worst[i] > 0:
            gsr_label = f'{avg_gsr_worst[i]:.2f}s'
            if avg_gsr_worst[i] > avg_curr_worst[i] and avg_curr_worst[i] > 0:
                factor = avg_gsr_worst[i] / avg_curr_worst[i]
                slower_opcodes = set(extract_opcode(op['name']) for op in vendor_gsr_ops[i] if op['seconds'] > avg_curr_worst[i])
                if slower_opcodes:
                    slower_ops_str = ', '.join(sorted(slower_opcodes))
                    gsr_label += f' ({factor:.1f}x slower: {slower_ops_str})'
            ax.text(bar2.get_width() + 0.05, bar2.get_y() + bar2.get_height()/2,
                   gsr_label, va='center', fontsize=9)
    
    ax.set_yticks(y_pos)
    ax.set_yticklabels(vendor_names, fontsize=10)
    ax.set_xlabel('Execution Time (seconds)')
    ax.set_ylabel('Hardware Vendor')
    ax.invert_yaxis()
    ax.grid(axis='x', alpha=0.3)
    
    legend_elements = [
        mpatches.Patch(facecolor=curr_color, alpha=0.8, label='Pre-GSR Worst Case (incl. Schnorr 80k sigs)'),
        mpatches.Patch(facecolor=gsr_color, alpha=0.8, label='New GSR Worst Case'),
    ]
    ax.legend(handles=legend_elements, loc='upper right')
    
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved: {output_path}")
    
    print("\nPER-VENDOR AVERAGED WORST CASES")
    print(f"{'Vendor':<20} {'Machines':<10} {'Pre-GSR':<15} {'New GSR':<15}")
    for i, vendor in enumerate(vendor_names):
        vendor_clean = vendor.split('\n')[0]
        print(f"{vendor_clean:<20} {machine_counts[i]:<10} {avg_curr_worst[i]:<15.3f} {avg_gsr_worst[i]:<15.3f}")
    print()


RESTORED_OPCODES = {
    'CAT', 'SUBSTR', 'LEFT', 'RIGHT', 'INVERT',
    'AND', 'OR', 'XOR', '2MUL', '2DIV',
    'MUL', 'DIV', 'MOD', 'LSHIFT', 'RSHIFT',
}


def extract_restored_opcode(name: str) -> str | None:
    """Return the restored opcode if the benchmark exercises one, else None."""
    parts = name.split('_')
    for part in parts:
        if part in RESTORED_OPCODES:
            # Disambiguate MUL/DIV from 2MUL/2DIV
            idx = parts.index(part)
            if part in ('MUL', 'DIV') and idx > 0 and parts[idx - 1].endswith('2'):
                continue
            return part
    return None


def create_restored_opcodes_visualization(all_machine_data: list[tuple], output_path: str):
    """Create a plot showing worst-case performance for each of the 15 restored opcodes."""
    if not all_machine_data:
        return

    # For each machine, get its pre-GSR worst case for normalization
    machine_pre_gsr_worst = []
    for results, _ in all_machine_data:
        current_script, _ = analyze_results(results)
        machine_pre_gsr_worst.append(current_script[0]['seconds'] if current_script else 1.0)

    # For each restored opcode, collect normalized worst-case (ratio to that machine's pre-GSR worst)
    opcode_machine_ratios = defaultdict(list)  # opcode -> [ratio per machine]

    for idx, (results, machine_info) in enumerate(all_machine_data):
        pre_gsr = machine_pre_gsr_worst[idx]
        opcode_worst = {}  # opcode -> worst seconds
        for r in results:
            op = extract_restored_opcode(r['name'])
            if op is None:
                continue
            if op not in opcode_worst or r['seconds'] > opcode_worst[op]:
                opcode_worst[op] = r['seconds']
        for op, t in opcode_worst.items():
            opcode_machine_ratios[op].append(t / pre_gsr if pre_gsr > 0 else 0)

    if not opcode_machine_ratios:
        print("No restored opcode benchmarks found.")
        return

    # Sort opcodes by mean ratio descending
    opcode_stats = []
    for op in RESTORED_OPCODES:
        ratios = opcode_machine_ratios.get(op, [])
        if not ratios:
            continue
        mean_r = sum(ratios) / len(ratios)
        opcode_stats.append((op, mean_r, ratios))
    opcode_stats.sort(key=lambda x: x[1], reverse=True)

    opcodes = [s[0] for s in opcode_stats]
    means = [s[1] for s in opcode_stats]
    all_ratios = [s[2] for s in opcode_stats]

    num_machines = len(all_machine_data)
    fig, ax = plt.subplots(figsize=(12, max(6, len(opcodes) * 0.5)))
    fig.suptitle('Worst Case Block Sized Script: Restored Opcodes (Script Restoration)\n'
                 f'Normalized to each machine\'s pre-GSR worst case — {num_machines} machine(s)',
                 fontsize=13, fontweight='bold')

    y_pos = range(len(opcodes))
    bar_color = '#8e44ad'
    ref_color = '#e74c3c'

    bars = ax.barh(y_pos, means, color=bar_color, alpha=0.8, height=0.6)

    for i, ratios in enumerate(all_ratios):
        ax.scatter(ratios, [i] * len(ratios), color=bar_color, alpha=0.4, s=30,
                   edgecolors='white', linewidth=0.5, zorder=5)

    ax.axvline(x=1.0, color=ref_color, linestyle='--', linewidth=2,
               label='1.0x = pre-GSR worst case')

    for i, (bar, mean_r) in enumerate(zip(bars, means)):
        ax.text(bar.get_width() + 0.01, bar.get_y() + bar.get_height() / 2,
                f'{mean_r:.2f}x', va='center', fontsize=9)

    ax.set_yticks(y_pos)
    ax.set_yticklabels([f'OP_{op}' for op in opcodes], fontsize=10)
    ax.set_xlabel('Fraction of pre-GSR worst case (per machine)', fontsize=11)
    ax.invert_yaxis()
    ax.grid(axis='x', alpha=0.3)

    from matplotlib.lines import Line2D
    legend_handles = [
        mpatches.Patch(facecolor=bar_color, alpha=0.8, label='Mean across machines'),
        Line2D([0], [0], marker='o', color='w', markerfacecolor=bar_color, alpha=0.4,
               markersize=6, label='Individual machines'),
        Line2D([0], [0], color=ref_color, linestyle='--', linewidth=2,
               label='1.0x = pre-GSR worst case'),
    ]
    ax.legend(handles=legend_handles, loc='lower right', fontsize=9)

    plt.tight_layout(rect=[0, 0, 1, 0.93])
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"Saved: {output_path}")


def main():
    if len(sys.argv) < 2:
        script_dir = Path(__file__).parent
        default_dir = script_dir / 'bench_varops_data'
        search_dir = default_dir if default_dir.is_dir() else Path('.')
        csv_paths = list(search_dir.glob('*.csv'))
        if not csv_paths:
            print(f"No CSV files found in {search_dir}.")
            print("Usage: python3 visualize_bench.py <csv_file1> [csv_file2] [csv_file3] ...")
            print("       Or provide a directory: python3 visualize_bench.py <directory>")
            print("       Or run with no arguments to automatically use all CSV files in bench_varops_data/.")
            sys.exit(1)
        csv_paths = [str(path) for path in csv_paths]
    else:
        first_arg = Path(sys.argv[1])
        if len(sys.argv) == 2 and first_arg.is_dir():
            csv_paths = list(first_arg.glob('*.csv'))
            if not csv_paths:
                print(f"No CSV files found in directory: {first_arg}")
                print("Usage: python3 visualize_bench.py <csv_file1> [csv_file2] [csv_file3] ...")
                print("       Or provide a directory: python3 visualize_bench.py <directory>")
                print("       Or run with no arguments to automatically use all CSV files in current directory.")
                sys.exit(1)
            csv_paths = [str(path) for path in csv_paths]
        else:
            csv_paths = sys.argv[1:]
    
    for path in csv_paths:
        if not Path(path).exists():
            print(f"Error: CSV file not found: {path}")
            sys.exit(1)

    filtered_paths = []
    skipped_paths = []
    for path in csv_paths:
        machine_info = extract_machine_info(path)
        cpu = machine_info.get('cpu', 'Unknown')
        year = get_cpu_year(cpu)
        if year is not None and year < CUTOFF_YEAR:
            skipped_paths.append((path, cpu, year))
        else:
            filtered_paths.append(path)

    if skipped_paths:
        print(f"Skipping {len(skipped_paths)} file(s) with CPU older than {CUTOFF_YEAR}:")
        for path, cpu, year in skipped_paths:
            print(f"  - {path} ({cpu}, {year})")

    csv_paths = filtered_paths
    if not csv_paths:
        print("No CSV files remain after year filtering.")
        sys.exit(1)

    print(f"Reading benchmark results from {len(csv_paths)} file(s):")
    for path in csv_paths:
        print(f"  - {path}")
    
    averaged_results, all_machine_data = parse_multiple_csvs(csv_paths)
    
    current_script, gsr_added = analyze_results(averaged_results)
    
    print_summary(current_script, gsr_added, len(csv_paths))
    
    plots_dir = Path(__file__).parent / 'plots'
    plots_dir.mkdir(exist_ok=True)

    output_base = 'benchmark_analysis' if len(csv_paths) > 1 else Path(csv_paths[0]).stem + '_analysis'
    create_averaged_visualization(current_script, gsr_added, len(csv_paths), str(plots_dir / f'{output_base}_seconds.png'))
    create_schnorr_equivalents_visualization(current_script, gsr_added, len(csv_paths), str(plots_dir / f'{output_base}_schnorr_units.png'))

    if len(csv_paths) > 1:
        create_per_machine_visualization(all_machine_data, str(plots_dir / f'{output_base}_per_machine.png'))
        create_vendor_grouped_visualization(all_machine_data, str(plots_dir / f'{output_base}_by_vendor.png'))
        create_machine_scatter_plot(all_machine_data, str(plots_dir / f'{output_base}_scatter.png'))
        create_restored_opcodes_visualization(all_machine_data, str(plots_dir / f'{output_base}_restored_opcodes.png'))
    else:
        create_per_machine_visualization(all_machine_data, str(plots_dir / f'{output_base}_machine.png'))
        create_machine_scatter_plot(all_machine_data, str(plots_dir / f'{output_base}_scatter.png'))
        create_restored_opcodes_visualization(all_machine_data, str(plots_dir / f'{output_base}_restored_opcodes.png'))


if __name__ == '__main__':
    main()
