import numpy as np
import os
import torch
from models.semantic_grid import SemanticGrid


def get_acc_proj_grid(ego_grid_sseg, pose, abs_pose, crop_size, cell_size):
    grid_dim = (ego_grid_sseg.shape[2], ego_grid_sseg.shape[3])
    # sg.sem_grid will hold the accumulated semantic map at the end of the episode (i.e. 1 map per episode)
    sg = SemanticGrid(1, grid_dim, crop_size[0], cell_size, spatial_labels=ego_grid_sseg.shape[1], object_labels=ego_grid_sseg.shape[1], ensemble_size=1)
    # Transform the ground projected egocentric grids to geocentric using relative pose
    geo_grid_sseg = sg.spatialTransformer(grid=ego_grid_sseg, pose=pose, abs_pose=abs_pose)
    # step_geo_grid contains the map snapshot every time a new observation is added
    step_geo_grid_sseg = sg.update_proj_grid_bayes(geo_grid=geo_grid_sseg.unsqueeze(0))
    # transform the projected grid back to egocentric (step_ego_grid_sseg contains all preceding views at every timestep)
    step_ego_grid_sseg = sg.rotate_map(grid=step_geo_grid_sseg.squeeze(0), rel_pose=pose, abs_pose=abs_pose)
    return step_ego_grid_sseg


def est_occ_from_depth(local3D, grid_dim, cell_size, device, occupancy_height_thresh=-0.9):
    ego_grid_occ = torch.zeros((1, 3, grid_dim[0], grid_dim[1]), dtype=torch.float32, device=device)
    for k in range(len(local3D)):
        local3D_step = local3D[k]

        # Habitat Z is backward, -depth in depth_to_3D call
        # Keep points for which z < 3m (to ensure reliable projection)
        # and points for which z > 0.5m (to avoid having artifacts right in-front of the robot)
        z = -local3D_step[:,2]
        # avoid adding points from the ceiling, threshold on y axis, y range is roughly [-1...2.5]
        y = local3D_step[:,1]
        local3D_step = local3D_step[(z < 3) & (z > 0.5) & (y < 1), :]

        # initialize all locations as unknown (void)
        occ_lbl = torch.zeros((local3D_step.shape[0], 1), dtype=torch.float32, device=device)

        if k == 0:
            # threshold height to get occupancy and free labels
            thresh = occupancy_height_thresh
            y = local3D_step[:,1]
            occ_lbl[y>thresh,:] = 1
            occ_lbl[y<=thresh,:] = 2
        else:
            # for the free particles, all are treated as free
            occ_lbl[:, 0] = 2

        # (N, 2) map coordinate for each 3D point
        map_coords = discretize_coords(x=local3D_step[:,0], z=local3D_step[:,2], grid_dim=grid_dim, cell_size=cell_size)

        ## Replicate label pooling
        grid = torch.empty(3, grid_dim[0], grid_dim[1], device=device)
        grid[:] = 1 / 3

        # If the robot does not project any values on the grid, then return the empty grid
        if map_coords.shape[0]==0:
            ego_grid_occ[0,:,:,:] = grid.unsqueeze(0)
            continue
        
        # (N, 3) - (x, y, label)
        concatenated = torch.cat([map_coords, occ_lbl.long()], dim=-1)
        unique_values, counts = torch.unique(concatenated, dim=0, return_counts=True)
        grid[unique_values[:, 2], unique_values[:, 1], unique_values[:, 0]] = counts + 1e-5

        if k == 0:
            ego_grid_occ[0, :, :, :] += grid / grid.sum(dim=0)
        else:
            ego_grid_occ[0, :, :, :] += 0.1 * grid / grid.sum(dim=0) # balanced by 0.1

    return ego_grid_occ

def est_occ_from_pcd(points:torch.Tensor, grid_dim, cell_size, lower_height, upper_height):
    """ 
    Estimate occupancy grid from point cloud data 
    Args:
        points: (N, 3) tensor
        grid_dim: (H, W) tuple
        cell_size: float
        device: torch device
        occupancy_height_thresh: float
    """
    world_grid_occ = torch.zeros((3, grid_dim[0], grid_dim[1]), dtype=torch.float32, device=points.device)
    
    # initialize all locations as unknown (void)
    occ_lbl = torch.zeros((points.shape[0], 1), dtype=torch.float32, device=points.device)

    y = points[:, 1]
    obstacle_sign = (y > lower_height) & (y < upper_height)
    occ_lbl[obstacle_sign, :] = 1
    free_sign = (y <= lower_height) | (y >= upper_height)
    occ_lbl[free_sign, :] = 2

    # (N, 2) map coordinate for each 3D point
    map_coords = discretize_coords(x=points[:,0], z=points[:,2], grid_dim=grid_dim, cell_size=cell_size)

    ## Replicate label pooling
    grid = torch.empty(3, grid_dim[0], grid_dim[1], device=points.device)
    grid[:] = 1 / 3

    # (N, 3) - (x, y, label)
    concatenated = torch.cat([map_coords, occ_lbl.long()], dim=-1)
    unique_values, counts = torch.unique(concatenated, dim=0, return_counts=True)
    grid[unique_values[:, 2], unique_values[:, 1], unique_values[:, 0]] = counts + 1e-5

    world_grid_occ = grid / grid.sum(dim=0)
    return world_grid_occ

def discretize_coords(x, z, grid_dim, cell_size, map_center = None, translation=0):
    # x, z are the coordinates of the 3D point (either in camera coordinate frame, or the ground-truth camera position)
    # If translation=0, assumes the agent is at the center
    # If we want the agent to be positioned lower then use positive translation. When getting the gt_crop, we need negative translation
    map_coords = torch.zeros((len(x), 2), device='cuda')
    if map_center is None:
        xb = torch.floor(x[:]/cell_size) + (grid_dim[0]-1)/2.0
        zb = torch.floor(z[:]/cell_size) + (grid_dim[1]-1)/2.0 + translation
    else:
        xb = torch.floor( ( x[:] - map_center[0] ) / cell_size) + (grid_dim[0]-1)/2.0
        zb = torch.floor( ( z[:] - map_center[1]) / cell_size) + (grid_dim[1]-1)/2.0
    xb = xb.int()
    zb = zb.int()
    map_coords[:,0] = xb
    map_coords[:,1] = zb
    # keep bin coords within dimensions
    map_coords[:, 0] = torch.clamp(map_coords[:, 0], 0, grid_dim[0]-1)
    map_coords[:, 1] = torch.clamp(map_coords[:, 1], 0, grid_dim[1]-1)
    return map_coords.long()

def get_gt_crops(abs_pose, pcloud, label_seq_all, agent_height, grid_dim, crop_size, cell_size):
    x_all, y_all, z_all = pcloud[0], pcloud[1], pcloud[2]
    episode_extend = abs_pose.shape[0]
    gt_grid_crops = torch.zeros((episode_extend, 1, crop_size[0], crop_size[1]), dtype=torch.int64)
    for k in range(episode_extend):
        # slice the gt map according to the agent height at every step
        x, y, label_seq = slice_scene(x_all.copy(), y_all.copy(), z_all.copy(), label_seq_all.copy(), agent_height[k])
        gt = get_gt_map(x, y, label_seq, abs_pose=abs_pose[k], grid_dim=grid_dim, cell_size=cell_size)
        _gt_crop = crop_grid(grid=gt.unsqueeze(0), crop_size=crop_size)
        gt_grid_crops[k,:,:,:] = _gt_crop.squeeze(0)
    return gt_grid_crops


def get_gt_map(x, y, label_seq, abs_pose, grid_dim, cell_size):
    # Transform the ground-truth map to align with the agent's pose
    # The agent is at the center looking upwards
    point_map = np.array([x,y])
    rot_mat_abs = np.array([[np.cos(-abs_pose[2]), -np.sin(-abs_pose[2])],[np.sin(-abs_pose[2]),np.cos(-abs_pose[2])]])
    trans_mat_abs = np.array([[-abs_pose[1]],[abs_pose[0]]]) #### This is important, the first index is negative.
    ##rotating and translating point map points
    t_points = point_map - trans_mat_abs
    rot_points = np.matmul(rot_mat_abs,t_points)
    x_abs = torch.tensor(rot_points[0,:], device='cuda')
    y_abs = torch.tensor(rot_points[1,:], device='cuda')

    map_coords = discretize_coords(x=x_abs, z=y_abs, grid_dim=grid_dim, cell_size=cell_size)

    true_seg_grid = torch.zeros((grid_dim[0], grid_dim[1], 1), device='cuda')
    true_seg_grid[map_coords[:,1], map_coords[:,0]] = label_seq

    ### We need to flip the ground truth to align with the observations.
    ### Probably because the -y tp -z is a rotation about x axis which also flips the y coordinate for matteport.
    true_seg_grid = torch.flip(true_seg_grid, dims=[0])
    true_seg_grid = true_seg_grid.permute(2, 0, 1)
    return true_seg_grid


def crop_grid(grid, crop_size):
    # Assume input grid is already transformed such that agent is at the center looking upwards
    grid_dim_h, grid_dim_w = grid.shape[2], grid.shape[3]
    cx, cy = int(grid_dim_w/2.0), int(grid_dim_h/2.0)
    rx, ry = int(crop_size[0]/2.0), int(crop_size[1]/2.0)
    top, bottom, left, right = cx-rx, cx+rx, cy-ry, cy+ry
    return grid[:, :, top:bottom, left:right]

def slice_scene(x, y, z, label_seq, height):
    # z = -z
    # Slice the scene below and above the agent
    below_thresh = height-0.2
    above_thresh = height+2.0 # ** should this be 0.2?
    all_inds = np.arange(y.shape[0])
    below_inds = np.where(z<below_thresh)[0]
    above_inds = np.where(z>above_thresh)[0]
    invalid_inds = np.concatenate( (below_inds, above_inds), 0) # remove the floor and ceiling inds from the local3D points
    inds = np.delete(all_inds, invalid_inds)
    x_fil = x[inds]
    y_fil = y[inds]
    label_seq_fil = torch.tensor(label_seq[inds], dtype=torch.float, device='cuda')
    return x_fil, y_fil, label_seq_fil


def get_explored_grid(grid_sseg, thresh=0.5):
    # Use the ground-projected ego grid to get observed/unobserved grid
    # Single channel binary value indicating cell is observed
    # Input grid_sseg T x C x H x W (can be either H x W or cH x cW)
    # Returns T x 1 x H x W
    T, C, H, W = grid_sseg.shape
    grid_explored = torch.ones((T, 1, H, W), dtype=torch.float32).to(grid_sseg.device)
    grid_prob_max = torch.amax(grid_sseg, dim=1)
    inds = torch.nonzero(torch.where(grid_prob_max<=thresh, 1, 0))
    grid_explored[inds[:,0], 0, inds[:,1], inds[:,2]] = 0
    return grid_explored